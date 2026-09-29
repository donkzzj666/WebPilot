"""One connection per unit of work; no process-local consistency locks."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class StorageBusyError(RuntimeError):
    """Bounded lock wait expired; the caller may retry the whole unit of work."""


@contextmanager
def connect(path: Path, *, busy_timeout_ms: int = 5000):
    if not 0 <= busy_timeout_ms <= 60000:
        raise ValueError("busy_timeout_ms must be between 0 and 60000")
    connection = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA synchronous=FULL")
        yield connection
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise StorageBusyError("Database busy; retry the complete short transaction") from error
        raise
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection):
    """Commit once, roll back any failure (including deferred FK commit errors).

    Keep network, browser, model and filesystem work outside this block. Never
    retry an arbitrary callback here: it could repeat an external side effect.
    """
    if connection.in_transaction:
        raise ValueError("Nested transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
