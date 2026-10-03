import pytest
from pydantic import ValidationError

from webagent.writes.models import WriteClaim, WriteCheckFacts, business_key


def claim(**changes):
    value = dict(identity_ref='account-fixture', target=dict(repository='Fixture/Project', branch='repair',
        base_sha='a' * 40, operation='commit', files=['src/z.py', 'src/a.py']),
        expected_change_sha256='b' * 64, precondition_version='before-v1', adapter_id='fixture-v1')
    return WriteClaim.model_validate({**value, **changes})


def test_semantic_key_normalizes_repository_and_file_order_and_excludes_attempt_metadata():
    first = claim()
    target = first.target.model_dump(mode='json')
    target.update(repository='fixture/project', files=['src/z.py', 'src/a.py'])
    second = claim(target=target, precondition_version='new-observation', adapter_id='fixture-v2')
    assert first.target.files == ['src/a.py', 'src/z.py']
    assert business_key('task-1', first) == business_key('task-1', second)
    assert business_key('task-2', first) != business_key('task-1', first)
    assert business_key('task-1', claim(identity_ref='other-account')) != business_key('task-1', first)
    assert business_key('task-1', claim(expected_change_sha256='c' * 64)) != business_key('task-1', first)


@pytest.mark.parametrize('changes', [{'run_id': 'new-run'}, {'epoch': 2}, {'expected_change_sha256': 'bad'},
                                   {'identity_ref': ''}, {'target': {'repository': 'wrong'}}])
def test_claim_rejects_unbounded_or_nonsemantic_model_fields(changes):
    with pytest.raises((ValidationError, ValueError)):
        claim(**changes)


@pytest.mark.parametrize('receipt', [{}, {'id': 'x' * 9000}, {'ids': list(range(33))},
                                   {'nested': {'a': {'b': {'c': {'d': {'e': 'x'}}}}}}])
def test_check_receipts_are_bounded(receipt):
    value = claim().model_dump(mode='json')
    value.pop('adapter_id')
    with pytest.raises(ValidationError):
        WriteCheckFacts.model_validate({**value, 'outcome': 'APPLIED', 'receipt': receipt,
                                       'snapshot_id': 'snapshot-1'})
