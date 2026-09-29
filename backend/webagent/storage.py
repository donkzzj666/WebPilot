"""Database ownership only. Business tables/migrations belong to M1-02."""

from contextlib import closing
import sqlite3

from .config import Settings


def initialize_business_storage(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(settings.business_db, timeout=5)) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise RuntimeError("Business database did not enter WAL mode")

