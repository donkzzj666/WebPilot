"""Read-only bindings for complete visible JSON and narrow direct arXiv pages.

This adapter locates fields in authenticated original bytes. It does not turn
candidate values, model summaries or display derivatives into source facts.
General HTML/PDF extraction and publication search belong to later adapters.
"""
from ..db import connect
from ..scheduler.store import validate_in_transaction
from ..verification.models import FieldBinding
from ..verification.rules import _leaves, _resolve, _MISSING, _bound_reference_ids
from ..verification.service import VerificationService


class StructuredJSONSourceAdapter:
    def __init__(self, data_dir, *, verifier=None):
        self.verifier = verifier or VerificationService(data_dir)

    def bindings(self, run_id, proposal, *, execution_token=None):
        ids = sorted(set(proposal.evidence_ids))
        if len(ids) > 256:
            raise ValueError('Too many source references')
        with self.verifier.evidence.files.locked(), connect(self.verifier.database) as db:
            if execution_token is not None:
                validate_in_transaction(db, execution_token)
                if execution_token.run_id != run_id:
                    raise ValueError('Source qualification belongs to another Run')
            documents, _ = self.verifier._documents(db, run_id, ids)
        items = proposal.items.model_dump(mode='json')
        paths = [path for path, _ in _leaves(items)]
        # These optional facts are compared independently by the verifier.
        paths.extend(path for path, _ in _leaves(proposal.coverage.model_dump(mode='json'), '/coverage'))
        bindings = []
        for doc in documents:
            if not doc.readable or doc.content is None:
                continue
            prefix = '/parsed_text' if isinstance(doc.content, dict) and 'parsed_text' in doc.content else ''
            if (proposal.items.scenario == 'research' and isinstance(doc.content, dict)
                    and 'arxiv_direct' in doc.content):
                prefix = '/arxiv_direct'
            for path in paths:
                if doc.evidence_id not in _bound_reference_ids(items, path, set(ids)):
                    continue
                if _resolve(doc.content, prefix + path) is not _MISSING:
                    bindings.append(FieldBinding(result_path=path, evidence_id=doc.evidence_id,
                                                 evidence_path=prefix + path))
            for name in ('source_kind', 'confirmed_boundary', 'list_items', 'detail_pages'):
                path = '/verification_context/' + name
                # Monitoring context pointers are fixed by the frozen verifier.
                if _resolve(doc.content, prefix + path) is not _MISSING:
                    bindings.append(FieldBinding(result_path='/context/' + name,
                                                 evidence_id=doc.evidence_id, evidence_path=prefix + path))
        # Coverage bindings are accepted only for the scenarios that verify it.
        if proposal.items.scenario not in ('research', 'monitoring'):
            bindings = [b for b in bindings if not b.result_path.startswith('/coverage/')]
        return bindings
