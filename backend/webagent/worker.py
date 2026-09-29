"""Independent idle Worker. Scheduling and leases start in M1-10."""

import asyncio
import json
import os
import signal

from .config import Settings, disable_external_tracing
from .runtime import check_runtime
from .storage import initialize_business_storage


async def run_worker(settings: Settings, *, once: bool = False) -> None:
    disable_external_tracing()
    # Third-party framework is imported only after inherited tracing is disabled.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    runtime = check_runtime()
    initialize_business_storage(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stopped.set)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute("PRAGMA busy_timeout=5000")
            await saver.conn.execute("PRAGMA synchronous=FULL")
            print(json.dumps({
                "event": "worker_ready", "service": "worker", "stage": "M1-01",
                "pid": os.getpid(), "task_execution_enabled": False,
                "sqlite_version": runtime["sqlite"], "mode": "idle",
            }), flush=True)
            if not once:
                await stopped.wait()
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
    print(json.dumps({"event": "worker_stopped", "service": "worker"}), flush=True)

