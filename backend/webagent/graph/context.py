"""Bounded current context with original artifacts kept outside graph state."""
from pathlib import Path

from ..db.repository import canonical_json
from ..errors import BusinessError
from ..evidence.service import EvidenceService
from ..models.schema import ModelInput
from .store import GraphStore


class ModelContextBuilder:
    def __init__(self, data_dir: Path, *, store=None, evidence=None):
        self.data_dir = Path(data_dir)
        self.store = store if store is not None else GraphStore(self.data_dir / 'business.sqlite3')
        self.evidence = evidence if evidence is not None else EvidenceService(self.data_dir)

    def build(self, run_id, snapshot_id, *, execution_token=None, expected_state_version=None) -> ModelInput:
        run = self.store.load_run(run_id)
        version = run['state_version'] if expected_state_version is None else expected_state_version
        # The observation remains exact; M1-17 adds actual object/version and
        # current ledger refs instead of a declared target-only checkpoint.
        checkpoint = self.store.checkpoint_observation(run_id,snapshot_id,
            expected_state_version=version,execution_token=execution_token)
        if execution_token is not None:
            from .recovery import RecoveryStore
            checkpoint = RecoveryStore(self.store.path).checkpoint_facts(run_id,execution_token,snapshot_id)
        # M1-14 already applies the bounded publication policy. Truncating or
        # editing its persisted view here would make the FILTERED label false.
        observation = self.evidence.store.filtered_observation(snapshot_id,run_id)['content']
        # A summary/display ref remains useful only while its immutable
        # provenance exists. Read originals locally for integrity, without
        # placing their bytes or IDs into the model context.
        for ref in checkpoint.evidence_ids:
            seen = set()
            try:
                while ref is not None:
                    if ref in seen or len(seen) >= 8:
                        raise ValueError('Invalid evidence provenance')
                    seen.add(ref)
                    metadata, _ = self.evidence.store.read(ref,run_id=run_id,allow_restricted=True)
                    ref = metadata['original_evidence_id']
            except (ValueError, BusinessError) as error:
                if isinstance(error,BusinessError) and error.code == 'EVIDENCE_STORAGE_UNAVAILABLE':
                    raise
                raise BusinessError('INPUT_BLOCKED','Evidence provenance is unavailable',status=409) from None
        image_ids = [ref for ref in observation['evidence_ids']
                     if self.evidence.store.metadata(ref,run_id=run_id)['artifact_kind'] == 'screenshot']
        payload = dict(run_id=run_id,contract=run['contract'].model_dump(mode='json'),
            observation=observation,verified_checkpoint=checkpoint.model_dump(mode='json'),
            image_evidence_ids=image_ids,allowed_action_schema_ref='urn:webagent:m0-contract-v1:Action',
            selected_flow_versions=[])
        model_input = ModelInput.model_validate_json(canonical_json(payload))
        images = self.evidence.model_images(snapshot_id,image_ids,run_id=run_id)
        self.evidence.guard_model_input(model_input.model_dump(mode='json'),images)
        # Only one current observation and one verified checkpoint cross this
        # boundary. Historical messages, proposals and raw artifacts stay local.
        return model_input
