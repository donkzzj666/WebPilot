"""Source bindings compare candidate leaves to authenticated original bytes."""
import asyncio
import json
from copy import deepcopy

import pytest

from webagent.db.repository import canonical_json
from webagent.graph.source import StructuredJSONSourceAdapter
from webagent.models.schema import ProposeResult
from webagent.verification.service import VerificationService
from storage.test_graph_store import setup_run
from storage.test_verification_service import prepared
from unit.test_verification_rules import setup


def case(tmp_path, *, text=None, scenario='finance'):
    _, proposal, documents, _ = setup(scenario)
    if text is None:
        text = canonical_json(documents[0].content)
    store, scheduler, token, evidence, view, _ = setup_run(tmp_path, text=text, scenario=scenario)
    payload = proposal.model_dump(mode='json')
    def refs(value):
        if isinstance(value, dict):
            return {k: view['evidence_ids'] if k == 'evidence_ids' else refs(v) for k, v in value.items()}
        return [refs(v) for v in value] if isinstance(value, list) else value
    proposal = ProposeResult.model_validate_json(canonical_json(refs(payload)))
    verifier = VerificationService(tmp_path, scheduler=scheduler)
    verifier.begin('run-1', 1)
    return proposal, verifier, view


def evaluate(proposal, verifier):
    bindings = StructuredJSONSourceAdapter(verifier.data_dir, verifier=verifier).bindings('run-1', proposal)
    record = asyncio.run(verifier.verify('run-1', proposal, bindings, expected_state_version=2))
    return bindings, record


def test_complete_visible_json_is_verified_from_original_envelope(tmp_path):
    proposal, verifier, view = case(tmp_path)
    bindings, record = evaluate(proposal, verifier)
    assert bindings and all(b.evidence_path.startswith('/parsed_text/') for b in bindings)
    assert all(c['verdict'] == 'PASS' for c in record['checks'])
    result = verifier.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'SUCCEEDED'
    assert result.evidence_ids == view['evidence_ids']


def test_candidate_value_does_not_select_or_create_its_own_source_fact(tmp_path):
    proposal, verifier, _ = case(tmp_path)
    payload = proposal.model_dump(mode='json')
    payload['items']['values'][0]['raw_value'] = '999999.99'
    proposal = ProposeResult.model_validate_json(canonical_json(payload))
    bindings, record = evaluate(proposal, verifier)
    assert bindings  # Same structural location, regardless of candidate equality.
    assert any(f['result_path'].endswith('/raw_value') and f['verdict'] == 'FAIL'
               for f in record['evaluation']['fields'])
    assert verifier.finalize(record['verification_id'], expected_state_version=2).outcome != 'SUCCEEDED'


@pytest.mark.parametrize('text', ['{"values": [], "values": []}', '{"values": NaN}',
    'prefix {"values": []}', '{"values": []} suffix', '[malformed', 'Plain report text'])
def test_ambiguous_or_partial_visible_json_cannot_prove_fields(tmp_path, text):
    proposal, verifier, _ = case(tmp_path, text=text)
    bindings, record = evaluate(proposal, verifier)
    assert bindings == []
    assert any(f['verdict'] == 'INSUFFICIENT' for f in record['evaluation']['fields'])
    assert verifier.finalize(record['verification_id'], expected_state_version=2).outcome != 'SUCCEEDED'


def test_model_or_display_derivative_cannot_override_original(tmp_path):
    proposal, verifier, view = case(tmp_path)
    item = verifier.evidence.metadata(view['evidence_ids'][0])
    original = verifier.evidence.metadata(item['original_evidence_id'])
    (tmp_path / original['artifact_path']).write_bytes(b'changed original')
    bindings, record = evaluate(proposal, verifier)
    assert bindings == []
    assert all(f['verdict'] != 'PASS' for f in record['evaluation']['fields'])


def test_ordinary_artifact_envelope_does_not_gain_browser_provenance(tmp_path):
    old = prepared(tmp_path, raw_transform=lambda b: canonical_json({'title': 'Claimed browser', 'text': b.decode()}).encode())
    verifier, proposal = old[0], old[1]
    bindings, record = evaluate(proposal, verifier)
    assert bindings == []
    assert verifier.finalize(record['verification_id'], expected_state_version=2).outcome != 'SUCCEEDED'


@pytest.mark.parametrize('scenario', ['research', 'monitoring'])
def test_visible_json_preserves_coverage_and_monitor_context_paths(tmp_path, scenario):
    proposal, verifier, _ = case(tmp_path, scenario=scenario)
    bindings, record = evaluate(proposal, verifier)
    assert bindings
    assert all(f['verdict'] == 'PASS' for f in record['evaluation']['fields'])
    if scenario == 'monitoring':
        assert any(b.result_path.startswith('/context/') and b.evidence_path.startswith('/parsed_text/verification_context/')
                   for b in bindings)
