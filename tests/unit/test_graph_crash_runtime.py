"""Graph reentry must use business facts and never replay pending tasks."""
import asyncio
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from webagent.db import connect
from webagent.errors import BusinessError
from webagent.graph.runtime import StateGraphAdapter
from webagent.gateway.service import BrowserGateway
from webagent.verification.service import VerificationService
from storage.test_graph_recovery import recovery_case, capture
from unit.test_gateway_service import FakeBrowser
from unit.test_graph_runtime import Model, graph_case


def ledger(path):
    with connect(path) as db:
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                for name in ('runs', 'task_events', 'quota_debits', 'run_budgets',
                             'budget_attempts', 'steps', 'graph_progress')}


def test_lost_ephemeral_action_requires_reconciliation_before_new_graph_input(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, gateway, verifier, model, graph = await graph_case(
                tmp_path, saver, model=Model(output='action'))
            async def crash(stage, run_id):
                if stage == 'action_before':
                    raise RuntimeError('owned synthetic process interruption')
            graph.fault_hook = crash
            with pytest.raises(RuntimeError):
                await graph.run(case[2])
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert saved.next == ('dispatch',)
            assert browser.execute_calls == 0 and len(model.inputs) == 1
            replacement = Model()
            rebuilt = StateGraphAdapter(tmp_path, gateway, replacement, verifier, checkpointer=saver)
            captures = browser.capture_calls
            with pytest.raises(BusinessError) as error:
                await rebuilt.run(case[2])
            assert error.value.code == 'STATE_CONFLICT'
            assert browser.capture_calls == captures and browser.execute_calls == 0
            assert not replacement.inputs
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('pending', ['dispatch', 'verify', 'aggregate', 'prepare_wait'])
def test_reconciled_fresh_input_discards_pending_side_effect_nodes(tmp_path, pending):
    async def exercise():
        case = recovery_case(tmp_path)
        recovery, scheduler, token, saved, session, _, _ = case
        browser = FakeBrowser(session.owner, session)
        browser.url = 'http://127.0.0.1:8765/finance'
        gateway = BrowserGateway(recovery.path, browser, scheduler=scheduler)
        model = Model()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, gateway, model,
                VerificationService(tmp_path, scheduler=scheduler), checkpointer=saver)
            config = {'configurable': {'thread_id': 'run-1'}}
            state = {**saved, 'route': 'wait' if pending == 'prepare_wait' else pending}
            await graph.graph.aupdate_state(config, state, as_node='decide')
            old = await graph.graph.aget_state(config)
            assert old.next == (pending,)
            recovery.begin(token, old.values)
            facts = capture(case)
            recovery.complete(token, 'snapshot-recovery', **facts)
            fresh = scheduler.reconcile('run-1', token.state_version)
            await graph.run(fresh)
            assert browser.execute_calls == browser.prepare_calls == 0
            assert browser.capture_calls == len(model.inputs) == 1
            saved_after = await graph.graph.aget_state(config)
            assert saved_after.next == ('wait',) and saved_after.values['wait_id']
            with connect(recovery.path) as db:
                assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('saved_wait', [False, True])
def test_unchecked_business_reconcile_does_not_bypass_recovery_barrier(tmp_path, saved_wait):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, gateway, verifier, model, graph = await graph_case(tmp_path, saver)
            if saved_wait:
                paused = await graph.run(case[2])
                case[1].resume('run-1', paused['state_version'])
            else:
                case[1].abandon(case[2])
            token = case[1].claim('worker-1', case[2].worker_generation)
            # Generic SchedulerStore's trusted adapter seam cannot serve as
            # the product graph's evidence-backed recovery receipt.
            unchecked = case[1].reconcile('run-1', token.state_version)
            before = (browser.capture_calls, browser.execute_calls, len(model.inputs))
            rebuilt = StateGraphAdapter(tmp_path, gateway, model, verifier, checkpointer=saver)
            with pytest.raises(BusinessError) as error:
                await rebuilt.run(unchecked)
            assert error.value.field == 'recovery_not_completed'
            assert (browser.capture_calls, browser.execute_calls, len(model.inputs)) == before
    asyncio.run(exercise())


def cancelled_graph(tmp_path, saver):
    async def prepare():
        case, browser, gateway, verifier, model, graph = await graph_case(tmp_path, saver)
        paused = await graph.run(case[2])
        case[1].resume('run-1', paused['state_version'])
        recovery = case[1].claim('worker-1', case[2].worker_generation)
        token = case[1].reconcile('run-1', recovery.state_version)
        case[1].finish(token, 'CANCELLED')
        return case, browser, model, graph
    return prepare()


def test_business_terminal_repairs_framework_only_and_is_idempotent(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, model, graph = await cancelled_graph(tmp_path, saver)
            before = ledger(case[0].business_db)
            calls = (browser.capture_calls, browser.execute_calls, len(model.inputs))
            # A terminal repair has no runtime clients or execution token.
            repair = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            summary = await repair.repair_terminals()
            assert summary == {'repaired': ['run-1'], 'blocked': []}
            saved = await repair.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert saved.values['completed'] and saved.values['route'] == 'end' and not saved.next
            assert ledger(case[0].business_db) == before
            assert (browser.capture_calls, browser.execute_calls, len(model.inputs)) == calls
            checkpoint_id = saved.config['configurable']['checkpoint_id']
            await repair.repair_terminal('run-1')
            again = await repair.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert again.config['configurable']['checkpoint_id'] == checkpoint_id
            assert ledger(case[0].business_db) == before
    asyncio.run(exercise())


@pytest.mark.parametrize('damage', ['event', 'schema', 'contract', 'run'])
def test_terminal_repair_reports_corrupt_graph_and_preserves_business(tmp_path, damage):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, model, graph = await cancelled_graph(tmp_path, saver)
            config = {'configurable': {'thread_id': 'run-1'}}
            saved = await graph.graph.aget_state(config)
            values = {**saved.values}
            if damage == 'event':
                values['business_event_id'] = 9223372036854775806
            elif damage == 'schema':
                values['state_schema_version'] = 'incompatible-version'
            elif damage == 'contract':
                values['contract_version'] += 1
            else:
                values['run_id'] = 'other-run'
            await graph.graph.aupdate_state(config, values, as_node='wait')
            before = ledger(case[0].business_db)
            repair = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            result = await repair.repair_terminals()
            assert not result['repaired'] and result['blocked'][0]['run_id'] == 'run-1'
            assert ledger(case[0].business_db) == before
            assert browser.execute_calls == 0 and len(model.inputs) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('damage', [None, 'event', 'contract', 'run'])
def test_initial_input_checkpoint_is_repaired_or_blocked_from_actual_refs(tmp_path, damage):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, gateway, verifier, model, graph = await graph_case(tmp_path, saver)
            actual_save = saver.aput
            stopped = False
            async def stop_after_initial(config, checkpoint, metadata, new_versions):
                nonlocal stopped
                if stopped:
                    raise RuntimeError('owned crash prevents subsequent saves')
                if '__start__' in checkpoint['channel_values']:
                    stopped = True
                    initial = {**checkpoint['channel_values']['__start__']}
                    if damage == 'event':
                        initial['business_event_id'] = 9223372036854775806
                    elif damage == 'contract':
                        initial['contract_version'] += 1
                    elif damage == 'run':
                        initial['run_id'] = 'other-run'
                    checkpoint = {**checkpoint, 'channel_values': {**checkpoint['channel_values'], '__start__': initial}}
                    await actual_save(config, checkpoint, metadata, new_versions)
                    raise RuntimeError('owned crash after first input checkpoint')
                return await actual_save(config, checkpoint, metadata, new_versions)
            saver.aput = stop_after_initial
            with pytest.raises(RuntimeError):
                await graph.run(case[2])
            saver.aput = actual_save
            config = {'configurable': {'thread_id': 'run-1'}}
            channels = (await saver.aget_tuple(config)).checkpoint['channel_values']
            assert '__start__' in channels and 'run_id' not in channels
            case[1].finish(case[2], 'CANCELLED')
            before = ledger(case[0].business_db)
            repair = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            result = await repair.repair_terminals()
            if damage:
                assert not result['repaired'] and result['blocked'][0]['run_id'] == 'run-1'
            else:
                assert result == {'repaired': ['run-1'], 'blocked': []}
                final = await repair.graph.aget_state(config)
                assert final.values['completed'] and not final.next
            assert ledger(case[0].business_db) == before
            assert browser.capture_calls == browser.execute_calls == 0 and not model.inputs
    asyncio.run(exercise())
