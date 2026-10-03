"""Trusted Worker composition over real isolated SQLite qualifications.

Browser and provider clients are narrow fakes here. The graph/browser HTTP
probe covers their real transports; these cases test lifecycle and authority.
"""
import asyncio
from dataclasses import replace
import json
import pickle
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from webagent.config import Settings
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.graph.executor import GraphExecutor
from webagent.graph import executor as executor_module
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from webagent.settings.models import ModelConnection, ModelSettingsRequest
from webagent.settings.secrets import CredentialError
from webagent.settings.service import create_configured_run, update_model
from webagent import worker as worker_module
from unit.test_verification_rules import setup


class Secrets:
    def __init__(self):
        self.values, self.reads = {}, []

    def put(self, reference, value):
        self.values[reference] = value

    def get(self, reference):
        self.reads.append(reference)
        if reference not in self.values:
            raise CredentialError('missing')
        return self.values[reference]

    def delete(self, reference):
        self.values.pop(reference, None)


class Manager:
    def __init__(self, settings):
        self.registry = SessionRegistry(settings.business_db)
        self.manager_id = 'executor-manager-' + uuid4().hex
        self.created, self.closed = [], []

    async def start(self):
        return self

    async def aclose(self):
        return None

    async def create(self, owner, **kwargs):
        self.created.append((owner, kwargs))
        record = self.registry.reserve(self.manager_id, owner,
            auth_ref=kwargs['auth_ref'], replaces=kwargs['replaces'],
            execution_token=kwargs['execution_token'])
        return self.registry.opened(record.session_id, self.manager_id,
                                    execution_token=kwargs['execution_token'])

    async def close(self, session_id, owner):
        self.registry.get(session_id, owner)
        self.closed.append(session_id)
        self.registry.closing(session_id, self.manager_id)
        return self.registry.closed(session_id, self.manager_id)

    async def close_preparation(self, session_id, owner, *, execution_token, paused_settlement=False):
        self.registry.closing_preparation(session_id, self.manager_id, owner,
            execution_token=execution_token, paused_settlement=paused_settlement)
        return await self.close(session_id, owner)

    async def close_terminal(self, session_id, owner):
        self.registry.closing_terminal(session_id, self.manager_id, owner)
        return await self.close(session_id, owner)


class Provider:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


def prepared(tmp_path, *, identity=None, scenario='finance', configured=False):
    settings = Settings(tmp_path)
    migrate(settings.business_db)
    contract = setup(scenario)[0].model_dump(mode='json')
    contract['identity_ref'] = identity if scenario != 'code' else 'test-identity'
    secrets = Secrets()
    with connect(settings.business_db) as db, transaction(db):
        create_task(db, task_id='task-1', instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        db.execute("UPDATE tasks SET preparation_status='READY',current_contract_version=1,requested_fields_json='[]' WHERE task_id='task-1'")
    if configured:
        update_model(settings.business_db, secrets, ModelSettingsRequest(
            expected_version=0, model=ModelConnection(), accept_data_sharing=True,
            api_key=SecretStr('SYNTHETIC_EXECUTOR_OLD_KEY')))
        create_configured_run(settings.business_db, secrets, expected_settings_version=1,
            run_id='run-1', task_id='task-1', contract_version=1,
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION)
    else:
        with connect(settings.business_db) as db, transaction(db):
            create_run(db, run_id='run-1', task_id='task-1', contract_version=1,
                graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
                model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
            db.execute("INSERT INTO run_budgets(budget_record_id,run_id) VALUES('budget-1','run-1')")
    store = SchedulerStore(settings.business_db, lease_seconds=300)
    resources = [Resource.site_identity(source['site_id'], contract['identity_ref']) for source in contract['sources']]
    resources.append(Resource.browser_context('run-1'))
    store.enqueue('run-1', resources, expected_state_version=0)
    generation = store.start_worker('worker-1')
    token = store.claim('worker-1', generation)
    manager = Manager(settings)
    return settings, store, token, manager, secrets


def current(settings):
    with connect(settings.business_db) as db:
        return dict(db.execute("SELECT state,state_version FROM runs WHERE run_id='run-1'").fetchone())


def injected(case, *, graph_action=None, **overrides):
    settings, store, token, manager, secrets = case
    provider, calls = Provider(), []

    def model(path, value):
        calls.append(('model', path, value))
        return 'model-adapter'

    def gateway(value, session, *, scheduler):
        calls.append(('gateway', value, session, scheduler))
        return 'browser-gateway'

    def verifier(data_dir, *, scheduler):
        calls.append(('verifier', data_dir, scheduler))
        return 'verifier'

    def graph(data_dir, gateway, model, verifier, *, source_adapter, checkpointer):
        calls.append(('graph', data_dir, gateway, model, verifier, source_adapter, checkpointer))
        async def run(token):
            calls.append(('run', token))
            if graph_action:
                return await graph_action(token)
            return store.defer(token, 'PAUSED')
        return SimpleNamespace(run=run)

    options = dict(scheduler=store, secret_store=secrets,
        provider_factory=lambda run_id: provider, model_adapter_factory=model,
        gateway_factory=gateway, verifier_factory=verifier, graph_factory=graph)
    options.update(overrides)
    return GraphExecutor(settings, manager, checkpointer='private-saver', **options), provider, calls


def test_trusted_clients_injected_only_at_runtime_and_wait_retains_context(tmp_path):
    case = prepared(tmp_path)
    executor, provider, calls = injected(case, source_adapter_factory=lambda contract, gateway: 'source-adapter')
    result = asyncio.run(executor(case[2]))
    assert result['state'] == 'PAUSED' and result['status'] == 'WAITING'
    assert [value[0] for value in calls] == ['gateway', 'model', 'verifier', 'graph', 'run']
    assert calls[3][1:] == (tmp_path, 'browser-gateway', 'model-adapter', 'verifier', 'source-adapter', 'private-saver')
    assert provider.closed == 1
    assert len(case[3].created) == 1 and not case[3].closed
    owner, options = case[3].created[0]
    assert owner.kind == 'run' and owner.owner_id == 'run-1'
    assert options['gateway_downloads'] is True and options['execution_token'] == case[2]
    with pytest.raises(TypeError):
        pickle.dumps(executor)


def test_terminal_graph_result_closes_client_and_managed_context(tmp_path):
    case = prepared(tmp_path)
    async def finish(token):
        return case[1].finish(token, 'CANCELLED')
    executor, provider, _ = injected(case, graph_action=finish)
    result = asyncio.run(executor(case[2]))
    assert result['state'] == 'CANCELLED'
    assert provider.closed == 1 and len(case[3].closed) == 1
    assert case[3].registry.list_owned(case[3].manager_id)[0].state == 'CLOSED'


def test_terminal_finally_keeps_human_window_after_budget_fence(tmp_path):
    from datetime import datetime, timedelta, timezone
    case = prepared(tmp_path)
    async def deadline_after_handoff(token):
        case[1].defer(token, 'WAITING_HANDOFF', control_owner='human',
            handoff_deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
        return case[1].expire_budget('run-1', 'handoff')
    executor, provider, _ = injected(case, graph_action=deadline_after_handoff)
    result = asyncio.run(executor(case[2]))
    assert result['state'] == 'FAILED' and provider.closed == 1
    assert not case[3].closed and case[3].registry.list_owned(case[3].manager_id)[0].state == 'OPEN'
    with connect(case[0].business_db) as db:
        assert db.execute("SELECT control_owner FROM resource_leases WHERE holder_run_id='run-1' AND resource_type='browser_context'").fetchone()[0] == 'human'


def test_wait_detaches_gateway_observers_without_closing_managed_context(tmp_path):
    case = prepared(tmp_path)
    browser = Provider()
    gateway = SimpleNamespace(browser=browser)
    executor, provider, calls = injected(case, gateway_factory=lambda *args, **kwargs: gateway)
    result = asyncio.run(executor(case[2]))
    assert result['state'] == 'PAUSED'
    assert browser.closed == provider.closed == 1
    assert len(case[3].created) == 1 and not case[3].closed
    assert case[3].registry.list_owned(case[3].manager_id)[0].state == 'OPEN'


def test_observer_cleanup_failure_still_closes_provider_and_terminal_context(tmp_path):
    case = prepared(tmp_path)
    async def unavailable():
        raise RuntimeError('Synthetic observer cleanup failure')
    gateway = SimpleNamespace(browser=SimpleNamespace(aclose=unavailable))
    async def finish(token):
        return case[1].finish(token, 'CANCELLED')
    executor, provider, calls = injected(case, graph_action=finish,
                                        gateway_factory=lambda *args, **kwargs: gateway)
    with pytest.raises(RuntimeError):
        asyncio.run(executor(case[2]))
    assert provider.closed == 1 and len(case[3].closed) == 1
    assert current(case[0])['state'] == 'CANCELLED'


def test_plain_graph_end_never_marks_business_success(tmp_path):
    case = prepared(tmp_path)
    async def end(token):
        return {'completed': True}
    executor, provider, _ = injected(case, graph_action=end)
    assert asyncio.run(executor(case[2])) == {'completed': True}
    assert current(case[0]) == {'state': 'RUNNING', 'state_version': 1}
    assert provider.closed == 1
    with connect(case[0].business_db) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['epoch', 'worker', 'generation', 'version'])
def test_stale_execution_qualification_blocks_all_client_allocation(tmp_path, change):
    case = prepared(tmp_path)
    executor, provider, calls = injected(case)
    updates = {'epoch': {'epoch': 2}, 'worker': {'worker_id': 'wrong'},
               'generation': {'worker_generation': 2}, 'version': {'state_version': 0}}[change]
    with pytest.raises(BusinessError):
        asyncio.run(executor(replace(case[2], **updates)))
    assert not calls and not case[3].created and not provider.closed


def test_authority_change_during_provider_lookup_prevents_context_allocation(tmp_path):
    case = prepared(tmp_path)
    provider = Provider()
    async def factory(run_id):
        case[1].defer(case[2], 'PAUSED')
        return provider
    executor, _, calls = injected(case, provider_factory=factory)
    with pytest.raises(BusinessError):
        asyncio.run(executor(case[2]))
    assert not calls and not case[3].created and provider.closed == 1


@pytest.mark.parametrize('code', ['CONFIG_NOT_READY', 'CONFIG_SNAPSHOT_MISSING', 'CONFIG_SNAPSHOT_INVALID'])
def test_configuration_failure_persists_wait_and_no_browser(tmp_path, code):
    case = prepared(tmp_path)
    def missing(run_id):
        raise BusinessError(code, 'Synthetic configuration failure', status=409)
    executor, _, calls = injected(case, provider_factory=missing)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'configuration_required'
    assert current(case[0]) == {'state': 'PAUSED', 'state_version': 2}
    assert not calls and not case[3].created
    with connect(case[0].business_db) as db:
        wait = db.execute("SELECT payload_json,state_version FROM task_events WHERE event_type='wait_registered'").fetchone()
        assert json.loads(wait['payload_json'])['wait_id'] == result['wait_id']
        assert wait['state_version'] == 2
        assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'WAITING'
        assert db.execute("SELECT count(*) FROM resource_leases WHERE resource_type='active_slot'").fetchone()[0] == 0


def test_missing_identity_persists_wait_before_any_provider_or_browser(tmp_path):
    case = prepared(tmp_path, identity='missing-identity')
    executor, provider, calls = injected(case)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'identity_recheck_required'
    assert current(case[0])['state'] == 'PAUSED'
    assert not calls and not case[3].created and provider.closed == 0


def identity_for(case, monkeypatch, **changes):
    identity = SimpleNamespace(state='VERIFIED', site_id='local-fixture', realm='public',
        origin='http://127.0.0.1:8765', auth_ref=None, requires_identity_check=True, **changes)
    monkeypatch.setattr(executor_module, 'IdentityStore',
        lambda path: SimpleNamespace(get_identity=lambda reference: identity))
    return identity


def test_old_verified_identity_is_not_current_browser_proof(tmp_path, monkeypatch):
    case = prepared(tmp_path, identity='test-identity')
    identity_for(case, monkeypatch)
    executor, provider, calls = injected(case)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'identity_recheck_required'
    assert not calls and not case[3].created and not provider.closed


@pytest.mark.parametrize('verified', [True, False])
def test_identity_recheck_uses_trusted_gateway_before_model_or_graph(tmp_path, monkeypatch, verified):
    case = prepared(tmp_path, identity='test-identity')
    identity = identity_for(case, monkeypatch)
    checks = []
    async def prepare(gateway, token, contract, restored):
        checks.append((gateway, token, contract.identity_ref, restored))
        return verified
    executor, provider, calls = injected(case, identity_preparer=prepare)
    result = asyncio.run(executor(case[2]))
    assert checks == [('browser-gateway', case[2], 'test-identity', identity)]
    assert provider.closed == 1
    assert case[3].created[0][0].identity_ref == 'test-identity'
    if verified:
        assert [item[0] for item in calls] == ['gateway', 'model', 'verifier', 'graph', 'run']
        assert not case[3].closed
    else:
        assert [item[0] for item in calls] == ['gateway']
        assert result['diagnostic'] == 'identity_recheck_required'
        assert len(case[3].closed) == 1


def test_wrong_origin_identity_never_reaches_recheck_or_browser(tmp_path, monkeypatch):
    case = prepared(tmp_path, identity='test-identity')
    identity = identity_for(case, monkeypatch)
    identity.origin = 'https://other.example'
    executor, provider, calls = injected(case, identity_preparer=lambda *args: True)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'identity_recheck_required'
    assert not calls and not case[3].created and not provider.closed


def test_same_manager_same_run_context_is_reused_after_explicit_reconciliation(tmp_path):
    case = prepared(tmp_path)
    executor, _, _ = injected(case)
    asyncio.run(executor(case[2]))
    paused = current(case[0])
    case[1].resume('run-1', paused['state_version'])
    recovery = case[1].claim('worker-1', case[2].worker_generation)
    # The test's read-only authority explicitly confirms the unchanged scoped
    # fixture context; framework restoration alone never calls reconcile.
    fresh = case[1].reconcile('run-1', recovery.state_version)
    executor, _, calls = injected(case)
    asyncio.run(executor(fresh))
    assert len(case[3].created) == 1 and not case[3].closed
    assert next(item[2] for item in calls if item[0] == 'gateway').owner.owner_id == 'run-1'


def test_repository_write_waits_for_trusted_adapter_without_any_action(tmp_path):
    case = prepared(tmp_path, scenario='code')
    executor, provider, calls = injected(case)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'write_adapter_unavailable'
    assert current(case[0])['state'] == 'PAUSED'
    assert not calls and not case[3].created and not provider.closed


def test_frozen_provider_resolves_original_config_after_settings_rotation(tmp_path, monkeypatch):
    case = prepared(tmp_path, configured=True)
    settings, store, token, manager, secrets = case
    old_ref = next(iter(secrets.values))
    update_model(settings.business_db, secrets, ModelSettingsRequest(
        expected_version=1, model=ModelConnection(max_tokens=512), accept_data_sharing=True,
        api_key=SecretStr('SYNTHETIC_EXECUTOR_NEW_KEY')))
    class Transport(Provider):
        def __init__(self, config, key):
            super().__init__()
            self.config, self.key = config, key
    from webagent.settings import service
    monkeypatch.setattr(service, 'DeepSeekTransport', Transport)
    secrets.reads.clear()
    executor, _, calls = injected(case, provider_factory=None)
    asyncio.run(executor(token))
    provider = next(call[2] for call in calls if call[0] == 'model')
    assert provider.key.get_secret_value() == 'SYNTHETIC_EXECUTOR_OLD_KEY'
    assert provider.closed == 1 and secrets.reads == [old_ref]
    with connect(settings.business_db) as db:
        assert db.execute('SELECT settings_version FROM run_config_snapshots').fetchone()[0] == 1


def test_missing_old_credential_never_uses_rotated_key(tmp_path):
    case = prepared(tmp_path, configured=True)
    settings, _, token, manager, secrets = case
    old_ref = next(iter(secrets.values))
    update_model(settings.business_db, secrets, ModelSettingsRequest(
        expected_version=1, model=ModelConnection(max_tokens=512), accept_data_sharing=True,
        api_key=SecretStr('SYNTHETIC_EXECUTOR_NEW_KEY')))
    secrets.delete(old_ref)
    secrets.reads.clear()
    executor, _, calls = injected(case, provider_factory=None)
    result = asyncio.run(executor(token))
    assert result['diagnostic'] == 'configuration_required'
    assert secrets.reads == [old_ref] and not manager.created and not calls


def test_browser_service_unavailable_pauses_and_closes_provider(tmp_path):
    case = prepared(tmp_path)
    async def unavailable(*args, **kwargs):
        raise BusinessError('SERVICE_UNAVAILABLE', 'Synthetic browser unavailable', status=503)
    case[3].create = unavailable
    executor, provider, calls = injected(case)
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'graph_preparation_failed'
    assert current(case[0])['state'] == 'PAUSED' and provider.closed == 1
    assert not calls


def test_invalid_saved_graph_blocks_recovery_before_browser_or_provider(tmp_path):
    case = prepared(tmp_path)
    generation = case[1].start_worker('worker-1')
    token = case[1].claim('worker-1', generation)
    assert current(case[0])['state'] == 'RECONCILING'
    def missing(run_id):
        raise BusinessError('CONFIG_NOT_READY', 'Synthetic missing old key', status=409)
    executor, _, calls = injected(case, provider_factory=missing)
    result = asyncio.run(executor(token))
    assert result['recovery_blocked'] and result['reason'] == 'graph_state_invalid'
    assert current(case[0])['state'] == 'RECONCILING'
    assert not calls and not case[3].created
    with connect(case[0].business_db) as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='wait_registered'").fetchone()[0] == 0
        assert db.execute('SELECT phase FROM graph_recoveries').fetchone()[0] == 'BLOCKED'
        assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'RECOVERY'
    assert case[1].claim('worker-1', generation) is None


def test_worker_registers_executor_with_independent_saver_without_model_configuration(tmp_path, monkeypatch, capsys):
    from webagent.sessions import manager as manager_module
    monkeypatch.setattr(manager_module, 'ManagedBrowser', Manager)
    asyncio.run(worker_module.run_worker(Settings(tmp_path), once=True))
    value = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if json.loads(line)['event'] == 'worker_ready')
    assert value['stage'] == 'M1-25' and value['executor_registered'] is True
    assert value['task_execution_enabled'] is True and value['model_ready'] is False
    assert value['mode'] == 'scheduled' and value['configured_tasks_only'] is True
    assert value['read_only_tasks_only'] is True
    with connect(tmp_path / 'graph.sqlite3') as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='checkpoints'").fetchone()


@pytest.mark.parametrize('change', ['human', 'generation'])
def test_revoked_identity_preparation_retains_context_for_new_owner(tmp_path, monkeypatch, change):
    from datetime import datetime, timedelta, timezone
    case = prepared(tmp_path, identity='test-identity')
    identity_for(case, monkeypatch)
    async def prepare(gateway, token, contract, identity):
        if change == 'human':
            case[1].defer(token, 'WAITING_HANDOFF', control_owner='human',
                handoff_deadline=datetime.now(timezone.utc) + timedelta(minutes=5))
        else:
            case[1].start_worker('worker-1')
        return True
    executor, provider, calls = injected(case, identity_preparer=prepare)
    with pytest.raises(BusinessError):
        asyncio.run(executor(case[2]))
    assert provider.closed == 1 and not case[3].closed
    assert case[3].registry.list_owned(case[3].manager_id)[0].state == 'OPEN'
    assert [item[0] for item in calls] == ['gateway']
