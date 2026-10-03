"""Independent browser owner and durable queue scheduler registration."""

import asyncio
import json
import os
import signal

from .config import Settings, disable_external_tracing
from .runtime import check_runtime
from .storage import initialize_business_storage
from .observability.logging import SafeJSONLLogger, TrustedGraphDiagnostics, safe_error_class


async def run_worker(settings: Settings, *, once: bool = False) -> None:
    disable_external_tracing()
    logger = SafeJSONLLogger(settings.data_dir / 'logs' / 'worker.jsonl')
    logger.emit('service_started', service='worker', pid=os.getpid())
    # Third-party framework is imported only after inherited tracing is disabled.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from .sessions.manager import ManagedBrowser
    from .identities.service import LoginService
    from .identities.rpc import LoginServer
    from .scheduler.store import SchedulerStore
    from .scheduler.worker import QueueWorker
    from .graph.executor import GraphExecutor
    from .graph.runtime import StateGraphAdapter
    from .settings.secrets import MacOSKeychainSecretStore
    from .settings.service import get_settings
    from .errors import BusinessError

    runtime = check_runtime()
    storage = initialize_business_storage(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stopped.set)
    sessions = ManagedBrowser(settings)
    login_server = None
    queue_worker = None
    try:
        await sessions.start()
        if not once:
            login_server = await LoginServer(settings, LoginService(settings, sessions)).start()
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute("PRAGMA busy_timeout=5000")
            await saver.conn.execute("PRAGMA synchronous=FULL")
            diagnostics = TrustedGraphDiagnostics(settings.data_dir, logger, checkpointer=saver)
            cleanup_graph = StateGraphAdapter(settings.data_dir, None, None, None,
                                              checkpointer=saver, diagnostics=diagnostics)
            terminal_recovery = await cleanup_graph.repair_terminals()
            if terminal_recovery['repaired'] or terminal_recovery['blocked']:
                print(json.dumps({'event': 'terminal_graph_recovery',
                    'repaired_count': len(terminal_recovery['repaired']),
                    'blocked_count': len(terminal_recovery['blocked'])}), flush=True)
            scheduler = SchedulerStore(settings.business_db)
            secrets = MacOSKeychainSecretStore()
            executor = GraphExecutor(settings, sessions, checkpointer=saver,
                                     scheduler=scheduler, secret_store=secrets, diagnostics=diagnostics)
            queue_worker = QueueWorker(scheduler, sessions.manager_id, executor=executor,
                controls=executor.controls, control_settler=executor.settle_control)
            await queue_worker.start()
            try:
                ready = await asyncio.to_thread(get_settings, settings.business_db, secrets)
                model_ready = ready['readiness']['ready']
            except BusinessError:
                model_ready = False
            print(json.dumps({
                "event": "worker_ready", "service": "worker", "stage": "M1-25",
                "pid": os.getpid(), "task_execution_enabled": True,
                "model_ready": model_ready,
                "sqlite_version": runtime["sqlite"],
                "mode": "scheduled",
                "business_schema_version": storage["schema_version"],
                "browser_sessions": "managed_lazy", "browser_headed_default": True,
                "browser_network": "authenticated_egress_proxy",
                "identity_preparation": "private_local_rpc" if not once else "not_started_once_mode",
                "budgets": "persistent_monotonic", "deadline_control": "independent",
                "scheduler": "durable_queue", "worker_generation": queue_worker.generation,
                "executor_registered": True, "configured_tasks_only": True,
                "read_only_tasks_only": True,
                "recovery": "business_reconciled_read_only", "checkpoint_durability": "sync",
            }), flush=True)
            logger.emit('worker_ready', service='worker', pid=os.getpid(),
                        worker_generation=queue_worker.generation)
            if not once:
                await queue_worker.run(stopped)
    except BaseException as error:
        logger.emit('service_failed', service='worker', error_class=safe_error_class(error))
        raise
    finally:
        try:
            try:
                if login_server is not None:
                    await login_server.aclose()
            finally:
                try:
                    if queue_worker is not None:
                        await queue_worker.aclose()
                finally:
                    await sessions.aclose()
        finally:
            for sig in signals:
                loop.remove_signal_handler(sig)
            logger.emit('worker_stopped', service='worker', pid=os.getpid())
    print(json.dumps({"event": "worker_stopped", "service": "worker"}), flush=True)
