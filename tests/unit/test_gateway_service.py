"""Gateway pipeline checks with real SQLite authority and a controlled browser double.

The double cannot write user data. Storage, epoch validation, quota charging,
intent records and timeout fencing use the production implementations.
"""
import asyncio
from dataclasses import replace
import hashlib
import struct
import zlib
import json
from types import SimpleNamespace
import time

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.models.schema import parse_model_output
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from webagent.tasks.compiler import compile_draft


URL = 'http://fixture.example/fixture'


def _fixture_png():
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 800, 600, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x80\x80\x80' * 800) * 600)) + chunk(b'IEND', b''))


PNG = _fixture_png()


def prepared(tmp_path, *, limits=None):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    now = utc_text()
    contract = compile_draft(
        {'instruction': 'Inspect synthetic gateway fixture', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'],
          'topic_criteria': ['fixture evidence'], 'cutoff_at': now, 'max_items': 3}},
        task_id='gateway-task', version=1, created_at=now,
        provenance=[{'origin': 'api', 'reference': 'gateway-pipeline-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['sources'] = [{'source_id': 'local-fixture', 'site_id': 'local-fixture',
                            'origin': 'http://fixture.example', 'path_prefix': '/fixture'}]
    contract['start_urls'] = [URL]
    contract['budget_profile'].update(limits or {})
    with connect(path) as db, transaction(db):
        create_task(db, task_id='gateway-task', instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='gateway-run', task_id='gateway-task', contract_version=1,
                   graph_version='gateway-test-v1', graph_state_schema_version='fixture-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    store = SchedulerStore(path)
    generation = store.start_worker('gateway-worker')
    store.enqueue('gateway-run', [Resource.site_identity('local-fixture', realm='webarena'),
                                  Resource.browser_context('gateway-run')],
                  expected_state_version=0, queue_class='webarena')
    token = store.claim('gateway-worker', generation)
    owner = SessionOwner('run', 'gateway-run', 'local-fixture', realm='webarena')
    registry = SessionRegistry(path)
    session = registry.reserve('gateway-manager', owner, execution_token=token)
    session = registry.opened(session.session_id, 'gateway-manager', execution_token=token)
    return path, store, token, owner, session


def action(token, snapshot, *, kind='scroll', step='step-one', target=None, args=None, **updates):
    value = {'run_id': token.run_id, 'step_id': step, 'epoch': token.epoch,
             'snapshot_id': snapshot['snapshot_id'], 'action_type': kind,
             'expected_effect': 'read',
             'target': target or {'page_url': snapshot['source_url'], 'tab_id': snapshot['tab_id'],
                                  'frame_id': snapshot['frame_id'], 'locator': None, 'write_scope': None},
             'args': args or {'direction': 'down', 'pixels': 100}}
    value.update(updates)
    return parse_model_output(json.dumps({'type': 'Action', 'action': value})).action


def rows(path, table):
    assert table in ('steps', 'observations', 'write_intents', 'resource_quarantines')
    with connect(path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


class FakeBrowser:
    """Imitate bounded browser calls without reproducing durable checks."""
    def __init__(self, owner, session, *, prepare_hook=None, execute_hook=None):
        self.owner = owner
        self.session_id = session.session_id
        self.managed = SimpleNamespace(manager_id=session.manager_id)
        self.prepare_hook, self.execute_hook = prepare_hook, execute_hook
        self.capture_calls = self.prepare_calls = self.execute_calls = 0
        self.version = 'page-one'
        self.width, self.height = 800, 600
        self.url = URL
        self.tab_id, self.frame_id = 'tab-one', 'frame-one'
        self.executed = []

    async def capture(self, token, *, include_screenshot=False, tab_id=None, frame_id=None):
        self.capture_calls += 1
        return {'page_url': self.url, 'title': 'Synthetic gateway page',
                'tab_id': tab_id or self.tab_id, 'frame_id': frame_id or self.frame_id,
                'page_version': self.version, 'width': self.width, 'height': self.height,
                'visible_text': 'Synthetic local fixture',
                'visible_sha256': hashlib.sha256(b'Synthetic local fixture').hexdigest(),
                'dom_sha256': hashlib.sha256(self.version.encode()).hexdigest(),
                'screenshot': PNG if include_screenshot else None,
                'screenshot_sha256': hashlib.sha256(PNG).hexdigest()
                                     if include_screenshot else None,
                'links': [], 'elements': []}

    async def prepare(self, token, value, snapshot, **kwargs):
        self.prepare_calls += 1
        if self.prepare_hook:
            await self.prepare_hook(token)
        if (snapshot['page_version'] != self.version or snapshot['width'] != self.width
                or snapshot['height'] != self.height or snapshot['tab_id'] != self.tab_id
                or snapshot['frame_id'] != self.frame_id or snapshot['source_url'] != self.url):
            raise BusinessError('STATE_CONFLICT', 'Synthetic observation changed', status=409)
        return SimpleNamespace(navigation=False, site_id='local-fixture', expected_effect='read',
                               mutating=False, classification='read', allowed_mutations=())

    async def execute(self, token, value, prepared, *, allowed_mutations=()):
        self.execute_calls += 1
        self.executed.append(value.step_id)
        if self.execute_hook:
            return await self.execute_hook(token)
        return {'dispatch_completed': True}


def gateway_for(tmp_path, *, limits=None, prepare_hook=None, execute_hook=None):
    from webagent.gateway.service import BrowserGateway
    path, store, token, owner, session = prepared(tmp_path, limits=limits)
    browser = FakeBrowser(owner, session, prepare_hook=prepare_hook, execute_hook=execute_hook)
    gateway = BrowserGateway(path, browser, scheduler=store, cancel_seconds=.05)
    return path, store, token, browser, gateway


def test_observation_is_persisted_and_blocked_until_evidence_filtering_exists(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token, include_screenshot=True)
        assert snapshot['redaction_status'] == 'BLOCKED'
        assert snapshot['visible_excerpt'] == ''
        assert snapshot['run_id'] == token.run_id and snapshot['epoch'] == token.epoch
        assert snapshot['session_id'] == browser.session_id
        assert len(rows(path, 'observations')) == 1
        assert store.budgets.status(token.run_id)['observations_used'] >= 1
        assert store.budgets.status(token.run_id)['screenshots_used'] >= 1
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('stale', ['epoch', 'worker', 'generation', 'state_version', 'run'])
def test_stale_qualification_rejected_before_browser_dispatch_or_charge(tmp_path, stale):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        changes = {'epoch': {'epoch': token.epoch + 1}, 'worker': {'worker_id': 'stale-worker'},
                   'generation': {'worker_generation': token.worker_generation + 1},
                   'state_version': {'state_version': token.state_version + 1},
                   'run': {'run_id': 'another-run'}}[stale]
        with pytest.raises(BusinessError):
            await gateway.dispatch(replace(token, **changes), action(token, snapshot))
        assert not browser.execute_calls and not rows(path, 'steps')
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


def test_pause_during_target_preparation_rechecks_authority_before_intent(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        async def pause(token):
            store.defer(token, 'PAUSED')
        browser.prepare_hook = pause
        snapshot = await gateway.observe(token)
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, action(token, snapshot))
        assert browser.prepare_calls == 1 and browser.execute_calls == 0
        assert rows(path, 'steps') == []
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


def test_preintent_permission_denial_keeps_run_available_for_a_safe_action(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        async def deny(token):
            raise BusinessError('FORBIDDEN', 'Synthetic target is outside policy', status=403)
        browser.prepare_hook = deny
        snapshot = await gateway.observe(token)
        with pytest.raises(BusinessError) as caught:
            await gateway.dispatch(token, action(token, snapshot, step='denied-target'))
        assert caught.value.code == 'FORBIDDEN' and rows(path, 'steps') == []
        store.validate(token)
        browser.prepare_hook = None
        snapshot = await gateway.observe(token)
        record = await gateway.dispatch(token, action(token, snapshot, step='safe-after-denial'))
        assert record['status'] == 'COMPLETED' and browser.execute_calls == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['page', 'viewport', 'tab', 'frame', 'url'])
def test_old_observation_cannot_dispatch_after_browser_context_changes(tmp_path, change):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        if change == 'page':
            browser.version = 'page-two'
        elif change == 'viewport':
            browser.width = 900
        elif change == 'tab':
            browser.tab_id = 'tab-two'
        elif change == 'frame':
            browser.frame_id = 'frame-two'
        else:
            browser.url = 'http://fixture.example/fixture/changed'
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, action(token, snapshot))
        assert browser.execute_calls == 0 and rows(path, 'steps') == []
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


def test_atomic_sequence_records_and_charges_each_of_ten_dispatches(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        builders = [lambda snapshot, index=index: action(token, snapshot, step=f'atomic-{index}')
                    for index in range(10)]
        results = await gateway.sequence(token, builders)
        assert len(results) == 10 and browser.execute_calls == 10
        assert store.budgets.status(token.run_id)['actions_used'] == 10
        assert len(rows(path, 'steps')) == 10
        assert [row['sequence'] for row in rows(path, 'steps')] == list(range(1, 11))
        assert all(row['status'] == 'COMPLETED' for row in rows(path, 'steps'))
    asyncio.run(exercise())


def test_duplicate_step_does_not_issue_a_second_physical_dispatch(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        value = action(token, snapshot)
        await gateway.dispatch(token, value)
        duplicate = await gateway.dispatch(token, value)
        assert duplicate['duplicate'] and not duplicate['dispatch_allowed']
        assert browser.execute_calls == 1 and len(rows(path, 'steps')) == 1
        assert store.budgets.status(token.run_id)['actions_used'] == 1
    asyncio.run(exercise())


def test_browser_timeout_keeps_attempt_and_uncertain_result_durable(tmp_path):
    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def hang(token):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        path, store, token, browser, gateway = gateway_for(tmp_path,
            limits={'action_timeout_seconds': 1}, execute_hook=hang)
        snapshot = await gateway.observe(token)
        with pytest.raises(BusinessError):
            await asyncio.wait_for(gateway.dispatch(token, action(token, snapshot)), 2)
        assert entered.is_set() and cancelled.is_set()
        assert browser.execute_calls == 1 and store.budgets.status(token.run_id)['actions_used'] == 1
        assert len(rows(path, 'steps')) == 1 and rows(path, 'steps')[0]['status'] == 'UNKNOWN'
    asyncio.run(exercise())


def test_independent_active_deadline_stops_a_hanging_browser_before_default_timeout(tmp_path):
    async def exercise():
        cancelled = asyncio.Event()
        async def hang(token):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        path, store, token, browser, gateway = gateway_for(tmp_path,
            limits={'max_active_seconds': 1}, execute_hook=hang)
        snapshot = await gateway.observe(token)
        with pytest.raises(BusinessError) as caught:
            await asyncio.wait_for(gateway.dispatch(token, action(token, snapshot)), 2)
        assert caught.value.code == 'BUDGET_EXCEEDED' and cancelled.is_set()
        assert store.budgets.status(token.run_id)['active_ms'] >= 1000
        assert rows(path, 'steps')[0]['status'] == 'UNKNOWN'
        with connect(path) as db:
            assert db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0] == 'FAILED'
    asyncio.run(exercise())


def test_preparation_and_execution_share_one_total_action_timeout(tmp_path):
    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def prepare_slowly(token):
            await asyncio.sleep(.6)
        async def hang(token):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        path, store, token, browser, gateway = gateway_for(tmp_path,
            limits={'action_timeout_seconds': 1}, prepare_hook=prepare_slowly, execute_hook=hang)
        snapshot = await gateway.observe(token)
        started = time.monotonic()
        with pytest.raises(BusinessError) as caught:
            await asyncio.wait_for(gateway.dispatch(token, action(token, snapshot)), 2)
        elapsed = time.monotonic() - started
        assert caught.value.code == 'TIMEOUT' and .9 <= elapsed < 1.45
        assert entered.is_set() and cancelled.is_set()
        assert store.budgets.status(token.run_id)['actions_used'] == 1
        assert rows(path, 'steps')[0]['status'] == 'UNKNOWN'
    asyncio.run(exercise())


def test_next_dispatch_past_atomic_cap_finishes_failed_and_fences_old_executor(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path, limits={'max_actions': 1})
        snapshot = await gateway.observe(token)
        assert (await gateway.dispatch(token, action(token, snapshot)))['status'] == 'COMPLETED'
        snapshot = await gateway.observe(token)
        with pytest.raises(BusinessError) as caught:
            await gateway.dispatch(token, action(token, snapshot, step='over-cap'))
        assert caught.value.code == 'BUDGET_EXCEEDED'
        assert browser.execute_calls == 1 and len(rows(path, 'steps')) == 1
        assert store.budgets.status(token.run_id)['actions_used'] == 1
        with connect(path) as db:
            assert db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0] == 'FAILED'
        with pytest.raises(BusinessError):
            store.validate(token)
    asyncio.run(exercise())


def test_result_arriving_after_pause_is_not_accepted_as_a_completed_action(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        async def finish_after_pause(token):
            store.defer(token, 'PAUSED')
            return {'dispatch_completed': True, 'raw_fixture': 'LATE_SYNTHETIC_RESULT'}
        browser.execute_hook = finish_after_pause
        snapshot = await gateway.observe(token)
        result = await gateway.dispatch(token, action(token, snapshot))
        assert result['status'] == 'UNKNOWN'
        assert not result['result_accepted'] and gateway.local_result('step-one') is None
        assert browser.execute_calls == 1 and store.budgets.status(token.run_id)['actions_used'] == 1
        assert 'LATE_SYNTHETIC_RESULT' not in json.dumps(rows(path, 'steps'))
    asyncio.run(exercise())


def test_sensitive_action_is_blocked_before_reservation_or_browser_preparation(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        value = action(token, snapshot, kind='navigate', args={'url': URL + '?api_key=sk-SYNTHETIC_SECRET_731'})
        with pytest.raises(BusinessError) as caught:
            await gateway.dispatch(token, value)
        assert caught.value.code == 'INPUT_BLOCKED'
        assert browser.prepare_calls == browser.execute_calls == 0
        assert not rows(path, 'steps')
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


def test_safe_page_fragment_is_preserved_in_action_and_model_binding(tmp_path):
    async def exercise():
        _, _, token, browser, gateway = gateway_for(tmp_path)
        browser.url = URL + '#section-42'
        snapshot = await gateway.observe(token)
        assert snapshot['source_url'] == browser.url
        model_view = gateway.model_observation(snapshot['snapshot_id'])
        assert model_view['source_url'] == browser.url
        assert model_view['redaction_status'] == 'FILTERED'
        for identifier in model_view['evidence_ids']:
            assert '#' not in gateway.evidence.display(identifier)[0]['source_url']
    asyncio.run(exercise())


def test_raw_action_receipt_is_restricted_and_journal_has_filtered_text(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        async def read_secret(token):
            return {'dispatch_completed': True, 'visible_text': 'Revenue 42; api_key=sk-SYNTHETIC_SECRET_731'}
        browser.execute_hook = read_secret
        snapshot = await gateway.observe(token)
        result = await gateway.dispatch(token, action(token, snapshot))
        assert result['result_accepted']
        assert 'sk-SYNTHETIC_SECRET_731' not in json.dumps(rows(path, 'steps'))
        with connect(path) as db:
            artifacts = [dict(row) for row in db.execute('SELECT * FROM evidence WHERE evidence_id IN '
                '(SELECT evidence_id FROM steps_evidence WHERE step_id=?)', ('step-one',))]
        assert len(artifacts) == 2
        original = next(item for item in artifacts if item['sensitivity'] == 'restricted')
        with pytest.raises(BusinessError) as caught:
            gateway.evidence.display(original['evidence_id'])
        assert caught.value.status == 403
        assert b'sk-SYNTHETIC_SECRET_731' in gateway.evidence.store.read(original['evidence_id'], allow_restricted=True)[1]
        display = next(item for item in artifacts if item['sensitivity'] == 'redacted')
        assert b'sk-SYNTHETIC_SECRET_731' not in gateway.evidence.display(display['evidence_id'])[1]
    asyncio.run(exercise())


def test_durable_storage_fault_blocks_old_qualification_before_new_browser_call(tmp_path):
    async def exercise():
        import errno
        from webagent.evidence.store import EvidenceStore
        path, _, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        def full(stage):
            if stage == 'mid_write':
                raise OSError(errno.ENOSPC, 'synthetic disk full')
        faulted = EvidenceStore(path.parent, fault_hook=full)
        with pytest.raises(BusinessError) as caught:
            faulted.publish(token.run_id, b'Synthetic capture', source_url=URL,
                captured_at=utc_text(), object_id='disk-full', query_scope='fixture',
                locator_or_page='viewport', execution_token=token)
        assert caught.value.status == 503
        capture_calls = browser.capture_calls
        with pytest.raises(BusinessError) as caught:
            await gateway.observe(token)
        assert caught.value.code == 'EVIDENCE_STORAGE_UNAVAILABLE'
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, action(token, snapshot))
        assert browser.capture_calls == capture_calls and browser.prepare_calls == browser.execute_calls == 0
        with pytest.raises(BusinessError):
            EvidenceStore(path.parent)
    asyncio.run(exercise())


def test_full_intent_journal_latches_fault_before_browser_effect(tmp_path, monkeypatch):
    async def exercise():
        import sqlite3
        path, _, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        def full(*args, **kwargs):
            raise sqlite3.OperationalError('database or disk is full')
        monkeypatch.setattr(gateway.store, 'prepare', full)
        with pytest.raises(BusinessError) as caught:
            await gateway.dispatch(token, action(token, snapshot))
        assert caught.value.code == 'EVIDENCE_STORAGE_UNAVAILABLE'
        assert browser.execute_calls == 0 and not rows(path, 'steps')
        captured = browser.capture_calls
        with pytest.raises(BusinessError):
            await gateway.observe(token)
        assert browser.capture_calls == captured
    asyncio.run(exercise())


def test_interrupted_filtered_capture_prevents_terminal_success(tmp_path, monkeypatch):
    async def exercise():
        from webagent.state import transition
        path, _, token, _, gateway = gateway_for(tmp_path)
        def interrupted(*args, **kwargs):
            raise RuntimeError('synthetic filter-index interruption')
        monkeypatch.setattr(gateway.evidence.store, 'record_filtered_observation', interrupted)
        with pytest.raises(RuntimeError):
            await gateway.observe(token)
        transition(path, run_id=token.run_id, expected_state_version=token.state_version, target='VERIFYING')
        with pytest.raises(BusinessError) as caught:
            transition(path, run_id=token.run_id, expected_state_version=token.state_version + 1, target='SUCCEEDED')
        assert caught.value.code == 'EVIDENCE_MISSING'
    asyncio.run(exercise())


def test_cancel_persists_unknown_and_revokes_qualification_before_browser_cancellation(tmp_path):
    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        observed_at_cancel = {}
        path, store, token, browser, gateway = gateway_for(tmp_path)
        async def hang(token):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                observed_at_cancel['status'] = rows(path, 'steps')[0]['status']
                try:
                    store.validate(token)
                except BusinessError:
                    observed_at_cancel['fenced'] = True
                cancelled.set()
        browser.execute_hook = hang
        snapshot = await gateway.observe(token)
        task = asyncio.create_task(gateway.dispatch(token, action(token, snapshot)))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert observed_at_cancel == {'status': 'UNKNOWN', 'fenced': True}
        assert store.budgets.status(token.run_id)['actions_used'] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('invalid', [
    {'action_type': 'javascript', 'args': {'script': 'alert(1)'}},
    {'args': {'direction': 'down', 'pixels': 100, 'script': 'alert(1)'}},
    {'args': {'direction': 'down', 'pixels': 100, 'http': 'https://fixture.example'}},
    {'args': {'direction': 'down', 'pixels': 100, 'sql': 'SELECT 1'}},
    {'args': {'direction': 'down', 'pixels': 100, 'shell': 'pwd'}},
])
def test_gateway_revalidates_untrusted_action_shapes_before_browser_calls(tmp_path, invalid):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        value = action(token, snapshot).model_dump(mode='json')
        value.update(invalid)
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, value)
        assert browser.prepare_calls == 0 and browser.execute_calls == 0
        assert rows(path, 'steps') == [] and store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())


def test_unleased_or_out_of_scope_navigation_rejected_before_dispatch(tmp_path):
    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        value = action(token, snapshot, kind='navigate', args={'url': 'http://fixture.example/outside'})
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, value)
        assert browser.execute_calls == 0 and rows(path, 'steps') == []
        assert store.budgets.status(token.run_id)['actions_used'] == 0
    asyncio.run(exercise())
