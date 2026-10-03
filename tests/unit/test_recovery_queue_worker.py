"""Durable task-level recovery blocks do not retry or stop unrelated work."""
import asyncio

from webagent.db import connect
from webagent.graph.recovery import RecoveryStore
from webagent.scheduler.worker import QueueWorker
from unit.test_graph_executor import prepared


def test_persisted_recovery_block_settles_worker_and_prevents_reclaim(tmp_path):
    async def exercise():
        settings, scheduler, original, _, _ = prepared(tmp_path)
        scheduler.abandon(original)
        token = scheduler.claim('worker-1', original.worker_generation)
        recovery = RecoveryStore(settings.business_db)
        calls = []
        async def executor(current):
            calls.append(current)
            recovery.begin(current)
            recovery.blocked(current, 'object_mismatch')
            scheduler.abandon(current)
            # Client cleanup may outlast the watchdog tick; only the durable
            # blocked receipt permits the same bounded tail as settlement.
            await asyncio.sleep(.05)
            return {'recovery_blocked': True}
        worker = QueueWorker(scheduler, 'worker-1', executor=executor,
            deadline_seconds=.002, heartbeat_seconds=.004, shutdown_seconds=.2)
        worker.generation = token.worker_generation
        await worker._execute(token)
        assert not worker._failed and len(calls) == 1
        assert scheduler.recovery_blocked_receipt(token)
        assert scheduler.claim('worker-1', token.worker_generation) is None
        with connect(settings.business_db) as db:
            assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'RECOVERY'
            assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM resource_leases WHERE resource_type='active_slot'").fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM resource_leases').fetchone()[0] > 0
        await worker.aclose()
    asyncio.run(exercise())


def test_forged_return_flag_without_business_receipt_stops_worker(tmp_path):
    async def exercise():
        settings, scheduler, token, _, _ = prepared(tmp_path)
        async def executor(current):
            return {'recovery_blocked': True}
        worker = QueueWorker(scheduler, 'worker-1', executor=executor)
        worker.generation = token.worker_generation
        await worker._execute(token)
        assert worker._failed and not scheduler.recovery_blocked_receipt(token)
        with connect(settings.business_db) as db:
            assert db.execute('SELECT count(*) FROM graph_recoveries').fetchone()[0] == 0
        await worker.aclose()
    asyncio.run(exercise())
