"""Fail early on unsupported Python/SQLite and prove WAL/FTS5 at startup."""

from contextlib import closing
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile


def sqlite_wal_fixed(version: tuple[int, ...]) -> bool:
    # Official WAL-reset fixes: https://www.sqlite.org/wal.html#walreset
    return (
        version >= (3, 51, 3)
        or (3, 44, 6) <= version < (3, 45, 0)
        or (3, 50, 7) <= version < (3, 51, 0)
    )


def check_runtime() -> dict:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("This dependency lock targets Python 3.12; use scripts/bootstrap.sh")
    if not sqlite_wal_fixed(sqlite3.sqlite_version_info):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} lacks the required WAL-reset fix; "
            "use >=3.51.3 or the official 3.44.6/3.50.7 patched branches"
        )
    with tempfile.TemporaryDirectory(prefix="webagent-runtime-") as directory:
        with closing(sqlite3.connect(Path(directory) / "probe.sqlite3")) as connection:
            wal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if wal != "wal":
                raise RuntimeError("SQLite WAL could not be enabled")
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
            connection.execute("INSERT INTO probe VALUES ('webagent capability')")
            if connection.execute(
                "SELECT count(*) FROM probe WHERE probe MATCH 'capability'"
            ).fetchone()[0] != 1:
                raise RuntimeError("SQLite FTS5 query failed")
            source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
    return {
        "python": platform.python_version(), "python_executable": sys.executable,
        "sqlite": sqlite3.sqlite_version, "sqlite_source_id": source_id,
        "wal": True, "fts5": True, "platform": platform.platform(),
    }

