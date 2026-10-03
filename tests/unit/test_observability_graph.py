"""Actual async StateGraph diagnostics over committed local fixture ledgers."""
import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from webagent.db import connect
from webagent.db.repository import canonical_json
from webagent.errors import BusinessError
from webagent.models.schema import ProposeResult
from webagent.graph.models import GraphSnapshot
from webagent.observability.logging import SafeJSONLLogger, TrustedGraphDiagnostics
from unit.test_graph_runtime import Model
from unit.test_graph_executor import prepared, injected
from unit.test_run_controls_runtime import graph_case
from unit.test_verification_rules import setup


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class ProposalModel(Model):
    async def generate(self, model_input, *, execution_token):
        self.inputs.append(model_input)
        body = setup()[1].model_dump(mode='json')
        body['evidence_ids'] = model_input.observation.evidence_ids
        body['items']['values'][0]['evidence_ids'] = model_input.observation.evidence_ids
        return SimpleNamespace(output=ProposeResult.model_validate_json(canonical_json(body)))


def test_actual_graph_success_and_verification_refs_are_local_diagnostics_not_business_events(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=ProposalModel())
            logger = SafeJSONLLogger(tmp_path / 'logs' / 'worker.jsonl')
            graph.diagnostics = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=saver)
            result = await graph.run(case[2])
            assert result['completed']
            rows = records(logger.path)
            assert any(row['event'] == 'graph_node_completed' and row['node'] == 'verify' for row in rows)
            assert any(row['node'] == 'verify' and row.get('verification_id') for row in rows if 'node' in row)
            assert any(row['event'] == 'graph_node_completed' and row['node'] == 'aggregate' for row in rows)
            assert rows[-1]['event'] == 'graph_checkpoint_saved'
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'SUCCEEDED'
                for row in rows:
                    assert row['run_id'] == row['thread_id'] == 'run-1' and row['task_id'] == 'task-1'
                    assert db.execute('SELECT 1 FROM task_events WHERE run_id=? AND event_id=?',
                                      (row['run_id'], row['event_id'])).fetchone()
                assert not db.execute("SELECT 1 FROM task_events WHERE event_type LIKE 'graph_node_%'").fetchone()
            for private in ('visible_text', 'prompt', 'response', 'cookie', 'api_key', 'execution_token'):
                assert private not in logger.path.read_text()
    asyncio.run(exercise())


def test_node_error_keeps_original_exception_and_never_formats_private_provider_text(tmp_path):
    class FailedModel(Model):
        async def generate(self, *args, **kwargs):
            raise failure
    failure = BusinessError('SERVICE_UNAVAILABLE', 'PRIVATE_PROVIDER_RESPONSE_CANARY', status=503)
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=FailedModel())
            logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
            graph.diagnostics = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=saver)
            with pytest.raises(BusinessError) as caught:
                await graph.run(case[2])
            assert caught.value is failure
            errors = [row for row in records(logger.path) if row['event'] == 'graph_node_failed']
            assert len(errors) == 1 and errors[0]['node'] == 'decide'
            assert errors[0]['error_code'] == 'SERVICE_UNAVAILABLE'
            saved = await saver.aget_tuple({'configurable': {'thread_id': 'run-1'}})
            checkpoint = records(logger.path)[-1]
            assert checkpoint['event'] == 'graph_checkpoint_saved'
            assert checkpoint['checkpoint_id'] == saved.checkpoint['id']
            assert checkpoint['event_id'] == saved.checkpoint['channel_values']['business_event_id']
            assert 'CANARY' not in logger.path.read_text()
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM task_events').fetchone()[0] == 1
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'RUNNING'
    asyncio.run(exercise())


def test_cancelled_invocation_keeps_original_cancel_and_reads_its_actual_checkpoint(tmp_path):
    async def exercise():
        entered = asyncio.Event()
        class CancelledModel(Model):
            async def generate(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=CancelledModel())
            logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
            graph.diagnostics = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=saver)
            job = asyncio.create_task(graph.run(case[2]))
            await asyncio.wait_for(entered.wait(), 1)
            job.cancel('PRIVATE_CANCELLATION_CANARY')
            with pytest.raises(asyncio.CancelledError) as caught:
                await job
            assert caught.value.args == ('PRIVATE_CANCELLATION_CANARY',)
            rows = records(logger.path)
            assert any(row['event'] == 'graph_node_interrupted' and row['node'] == 'decide' for row in rows)
            assert not any(row['event'] == 'graph_node_failed' for row in rows)
            saved = await saver.aget_tuple({'configurable': {'thread_id': 'run-1'}})
            assert rows[-1]['event'] == 'graph_checkpoint_saved'
            assert rows[-1]['checkpoint_id'] == saved.checkpoint['id']
            assert rows[-1]['event_id'] == saved.checkpoint['channel_values']['business_event_id']
            assert 'CANARY' not in logger.path.read_text()
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM task_events').fetchone()[0] == 1
                assert not db.execute('SELECT 1 FROM run_results').fetchone()
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'RUNNING'
    asyncio.run(exercise())


def test_node_entry_preserves_real_checkpoint_when_budget_failure_tail_is_cancelled(tmp_path):
    async def exercise():
        tail_entered = asyncio.Event()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=Model(output='evidence'))
            logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
            class TailDiagnostics(TrustedGraphDiagnostics):
                async def checkpoint(self, run_id, **kwargs):
                    with connect(case[0].business_db) as db:
                        terminal = db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] == 'FAILED'
                    if terminal:
                        tail_entered.set()
                        await asyncio.Event().wait()
                    return await super().checkpoint(run_id, **kwargs)
            graph.diagnostics = TailDiagnostics(tmp_path, logger, checkpointer=saver)
            job = asyncio.create_task(graph.run(case[2]))
            try:
                await asyncio.wait_for(tail_entered.wait(), 3)
                # The production budget admission and expiration have already
                # fenced this Run. Cancel the blocked tail exactly as an outer
                # watchdog can; do not shield or rewrite the framework save.
                with connect(case[0].business_db) as db:
                    run = db.execute('SELECT state,state_version FROM runs').fetchone()
                    assert tuple(run) == ('FAILED', 2)
                    assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'FINISHED'
                    before = [tuple(row) for row in db.execute(
                        'SELECT event_id,state_version,event_type,payload_json FROM task_events ORDER BY event_id')]
                    assert not db.execute('SELECT 1 FROM run_results').fetchone()
                assert case[1].budgets.status('run-1')['reason'] == 'recovery_limit'
                job.cancel('PRIVATE_DEADLINE_CANCELLATION_CANARY')
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await job
                assert cancelled.value.args == ('PRIVATE_DEADLINE_CANCELLATION_CANARY',)
                saved = await saver.aget_tuple({'configurable': {'thread_id': 'run-1'}})
                state = saved.checkpoint['channel_values']
                assert state['state_version'] == 1 and not state['completed']
                checkpoints = [row for row in records(logger.path) if row['event'] == 'graph_checkpoint_saved']
                assert any(row['checkpoint_id'] == saved.checkpoint['id']
                    and row['event_id'] == state['business_event_id']
                    and row['state_version'] == state['state_version']
                    and row['progress_id'] == state['progress_id'] for row in checkpoints)
                assert all(row['state_version'] == 1 for row in checkpoints)
                assert 'CANARY' not in logger.path.read_text()
                with connect(case[0].business_db) as db:
                    assert before == [tuple(row) for row in db.execute(
                        'SELECT event_id,state_version,event_type,payload_json FROM task_events ORDER BY event_id')]
            finally:
                if not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
    asyncio.run(exercise())


def test_framework_interrupt_is_not_reported_as_node_or_business_failure(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=Model())
            logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
            graph.diagnostics = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=saver)
            result = await graph.run(case[2])
            rows = records(logger.path)
            assert result['wait_id'] and not result['completed']
            assert any(row['event'] == 'graph_node_interrupted' and row['node'] == 'wait' for row in rows)
            assert not any(row['event'] == 'graph_node_failed' for row in rows)
            assert rows[-1]['event'] == 'graph_checkpoint_saved'
    asyncio.run(exercise())


@pytest.mark.parametrize('corruption', ['thread', 'graph_version', 'event', 'checkpoint', 'raw_channel', 'metadata', 'wait_progress'])
def test_arbitrary_framework_payloads_cannot_become_diagnostics_or_business_events(tmp_path, corruption):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=Model())
            await graph.run(case[2])
            config = {'configurable': {'thread_id': 'run-1'}}
            original = await saver.aget_tuple(config)
            checkpoint, metadata, config = deepcopy(original.checkpoint), deepcopy(original.metadata), deepcopy(original.config)
            channels = checkpoint['channel_values']
            if corruption == 'thread':
                config['configurable']['thread_id'] = 'another-run'
            elif corruption == 'graph_version':
                channels['graph_version'] = 'spoofed-private-framework-canary'
            elif corruption == 'event':
                channels['business_event_id'] = 999999
            elif corruption == 'checkpoint':
                config['configurable']['checkpoint_id'] = 'spoofed-private-framework-canary'
            elif corruption == 'raw_channel':
                channels['stream_payload'] = {'prompt': 'PRIVATE_FRAMEWORK_CANARY'}
            elif corruption == 'wait_progress':
                channels['progress_id'] = None
            else:
                metadata['prompt'] = 'PRIVATE_FRAMEWORK_CANARY'
            fake = original._replace(checkpoint=checkpoint, metadata=metadata, config=config)
            class Saver:
                async def aget_tuple(self, config):
                    return fake
            logger = SafeJSONLLogger(tmp_path / 'framework.jsonl')
            adapter = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=Saver())
            with connect(case[0].business_db) as db:
                before = db.execute('SELECT count(*) FROM task_events').fetchone()[0]
            assert not await adapter.checkpoint('run-1') and not logger.path.exists()
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM task_events').fetchone()[0] == before
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['logger', 'checkpoint'])
def test_failed_logger_and_checkpoint_lookup_cannot_replace_a_valid_business_result(tmp_path, failure):
    class Logger:
        def emit(self, *args, **kwargs):
            raise OSError('PRIVATE_LOG_FAILURE_CANARY')
    class FailedCheckpoint(TrustedGraphDiagnostics):
        async def checkpoint(self, *args, **kwargs):
            raise OSError('PRIVATE_CHECKPOINT_LOOKUP_CANARY')
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=ProposalModel())
            graph.diagnostics = (TrustedGraphDiagnostics(tmp_path, Logger(), checkpointer=saver)
                if failure == 'logger' else FailedCheckpoint(tmp_path,
                    SafeJSONLLogger(tmp_path / 'worker.jsonl'), checkpointer=saver))
            result = await graph.run(case[2])
            assert result['completed']
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'SUCCEEDED'
    asyncio.run(exercise())


def test_slow_diagnostic_writer_does_not_block_the_async_graph_or_its_deadline(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowDiagnostics(TrustedGraphDiagnostics):
        def node(self, run_id, node, phase, *, error=None):
            if node == 'reconcile' and phase == 'started':
                entered.set()
                release.wait(2)
                return False
            return super().node(run_id, node, phase, error=error)
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=ProposalModel())
            graph.diagnostics = SlowDiagnostics(tmp_path, SafeJSONLLogger(tmp_path / 'worker.jsonl'))
            job = asyncio.create_task(graph.run(case[2]))
            try:
                async with asyncio.timeout(.1):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                # The first writer stays blocked; the hook's independent
                # bound lets the actual graph proceed to its real result.
                result = await asyncio.wait_for(job, 1.5)
                assert result['completed'] and not release.is_set()
                with connect(case[0].business_db) as db:
                    assert db.execute('SELECT state FROM runs').fetchone()[0] == 'SUCCEEDED'
            finally:
                release.set()
                if not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
    asyncio.run(exercise())


def test_executor_injects_same_adapter_and_logs_the_original_failure_from_committed_run(tmp_path):
    case = prepared(tmp_path)
    logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
    diagnostics = TrustedGraphDiagnostics(tmp_path, logger)
    failure = BusinessError('UPSTREAM_ERROR', 'PRIVATE_EXECUTOR_PROVIDER_CANARY')
    def graph_factory(*args, diagnostics, **kwargs):
        assert diagnostics is adapter
        async def run(token):
            raise failure
        return SimpleNamespace(run=run)
    adapter = diagnostics
    executor, provider, _ = injected(case, graph_factory=graph_factory, diagnostics=diagnostics)
    with pytest.raises(BusinessError) as caught:
        asyncio.run(executor(case[2]))
    assert caught.value is failure and provider.closed == 1
    errors = records(logger.path)
    assert len(errors) == 1 and errors[0]['event'] == 'graph_executor_failed'
    assert errors[0]['run_id'] == 'run-1' and errors[0]['event_id'] == 1
    assert errors[0]['error_class'] == 'provider' and 'CANARY' not in logger.path.read_text()
    with connect(case[0].business_db) as db:
        assert not db.execute('SELECT 1 FROM run_results').fetchone()


def test_missing_business_database_is_not_created_by_node_or_checkpoint_diagnostics(tmp_path):
    checkpoint_id = str(uuid4())
    class Saver:
        async def aget_tuple(self, config):
            return SimpleNamespace(config={'configurable': {'thread_id': 'run-1',
                'checkpoint_id': checkpoint_id, 'checkpoint_ns': ''}},
                checkpoint={'id': checkpoint_id, 'channel_values': GraphSnapshot(
                    run_id='run-1', contract_version=1, state_version=1, business_event_id=1).state()},
                metadata={'source': 'loop', 'step': 1, 'thread_id': 'run-1'})
    logger = SafeJSONLLogger(tmp_path / 'logs' / 'worker.jsonl')
    diagnostics = TrustedGraphDiagnostics(tmp_path, logger, checkpointer=Saver())
    assert not diagnostics.node('run-1', 'observe', 'started')
    assert not asyncio.run(diagnostics.checkpoint('run-1'))
    assert not (tmp_path / 'business.sqlite3').exists() and not logger.path.exists()
