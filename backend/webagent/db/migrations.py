"""Versioned, checksummed, all-or-nothing migrations of the business DB only."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import time

from .connection import StorageBusyError, connect, transaction

APPLICATION_ID = 0x57414231  # WAB1; never claim a graph or arbitrary SQLite file.
SQL_DIR = Path(__file__).with_name("sql")
MIGRATIONS = ("0001_core.sql", "0002_resources_quotas.sql", "0003_state_events.sql", "0004_task_api.sql", "0005_model_calls.sql", "0006_settings_snapshots.sql", "0007_task_compilations.sql", "0008_browser_sessions.sql", "0009_identities.sql", "0010_scheduler.sql", "0011_budgets.sql", "0012_gateway.sql", "0013_evidence.sql", "0014_verification.sql", "0015_graph_runtime.sql", "0016_graph_recovery.sql", "0017_run_controls.sql", "0018_write_protocol.sql")
LATEST_VERSION = len(MIGRATIONS)


class MigrationError(RuntimeError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def schema_digest(db):
    return digest(repr([tuple(row) for row in db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )]))


def statements(script):
    # complete_statement understands trigger bodies; splitting on ';' does not.
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise MigrationError("Incomplete migration SQL")


def inspect(db, scripts):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    app_id = db.execute("PRAGMA application_id").fetchone()[0]
    objects = db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if app_id == 0 and version == 0 and not objects:
        return 0
    if app_id != APPLICATION_ID:
        raise MigrationError("Not a WebAgent business database; refusing to migrate")
    if not 1 <= version <= len(scripts):
        raise MigrationError("Unknown/newer database version; refusing downgrade")
    try:
        history = db.execute("SELECT version, name, sha256, schema_sha256 FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.DatabaseError as error:
        raise MigrationError("Missing migration history") from error
    if [row['version'] for row in history] != list(range(1, version + 1)):
        raise MigrationError("Migration history is not contiguous")
    for row in history:
        name, script = scripts[row['version'] - 1]
        if row['name'] != name or row['sha256'] != digest(script):
            raise MigrationError("Applied migration checksum differs from source")
    if history[-1]['schema_sha256'] != schema_digest(db):
        raise MigrationError("Database schema drift detected")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("Existing foreign key violation")
    return version


def _migrate_once(path: Path, *, target: int | None = None, busy_timeout_ms: int = 5000) -> dict:
    scripts = [(name, (SQL_DIR / name).read_text()) for name in MIGRATIONS]
    target = len(scripts) if target is None else target
    if type(target) is not int or not 1 <= target <= len(scripts):
        raise MigrationError("Target must be a known positive schema version")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect(path, busy_timeout_ms=busy_timeout_ms) as db:
        # Refuse foreign files before changing their journal mode.
        db.execute("BEGIN")
        inspected_version = inspect(db, scripts)
        db.execute("COMMIT")
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise MigrationError("Business database must support WAL")
        # SQL17 widens a parent table CHECK using SQLite's standard rebuild.
        # FK enforcement must be changed before BEGIN; checks remain explicit
        # on both the old schema and the complete provisional replacement.
        rebuild_events = inspected_version < 17 <= target
        if rebuild_events:
            db.execute('PRAGMA foreign_keys=OFF')
        try:
            with transaction(db):
                # Repeat after taking the SQLite write lock: another process
                # may have migrated while this one waited.
                before = inspect(db, scripts)
                if target < before:
                    raise MigrationError("Downgrades are not supported")
                if before == 0:
                    db.execute("""CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                        sha256 TEXT NOT NULL, schema_sha256 TEXT NOT NULL,
                        applied_at TEXT NOT NULL) STRICT""")
                    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                for version in range(before + 1, target + 1):
                    name, script = scripts[version - 1]
                    for statement in statements(script):
                        db.execute(statement)
                    db.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
                               (version, name, digest(script), schema_digest(db),
                                datetime.now(timezone.utc).isoformat(timespec="microseconds").replace('+00:00', 'Z')))
                    db.execute(f"PRAGMA user_version={version}")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise MigrationError("Migration introduced foreign key violations")
        finally:
            if rebuild_events:
                db.execute('PRAGMA foreign_keys=ON')
        return {"previous_version": before, "schema_version": target, "applied": target - before}


def migrate(path: Path, *, target: int | None = None, busy_timeout_ms: int = 5000) -> dict:
    """Retry only rolled-back migration transactions within a bounded deadline.

    Concurrent WAL bootstrap can report BUSY without honoring busy_timeout.
    Retrying this local, transactional operation is safe; application writes
    are never replayed by this mechanism.
    """
    if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 60000:
        raise ValueError("busy_timeout_ms must be between 0 and 60000")
    deadline = time.monotonic() + busy_timeout_ms / 1000
    while True:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            return _migrate_once(path, target=target, busy_timeout_ms=remaining)
        except StorageBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))
