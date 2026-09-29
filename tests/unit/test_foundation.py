import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from webagent.api import create_app
from webagent.config import Settings, disable_external_tracing
from webagent.runtime import check_runtime, sqlite_wal_fixed
from webagent.storage import initialize_business_storage


@pytest.mark.parametrize("version, expected", [
    ((3, 43, 2), False), ((3, 44, 5), False), ((3, 44, 6), True),
    ((3, 49, 9), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 2), False), ((3, 51, 3), True), ((3, 53, 1), True),
])
def test_wal_fix_is_not_a_simple_minimum(version, expected):
    assert sqlite_wal_fixed(version) is expected


def test_actual_runtime_capabilities():
    assert check_runtime()["fts5"] is True


def test_storage_creates_no_business_schema_and_does_not_touch_graph(tmp_path):
    settings = Settings(tmp_path)
    initialize_business_storage(settings)
    initialize_business_storage(settings)
    assert settings.business_db != settings.graph_db
    assert not settings.graph_db.exists()
    with sqlite3.connect(settings.business_db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_api_health_does_not_claim_worker_or_task_success(tmp_path):
    with TestClient(create_app(Settings(tmp_path))) as client:
        result = client.get("/health")
        assert result.status_code == 200
        assert result.json()["task_execution_enabled"] is False
        assert result.json()["storage"]["graph"] == "owned_by_worker"
        assert client.post("/v1/tasks", json={"instruction": "example"}).status_code == 404
    assert not (tmp_path / "graph.sqlite3").exists()


def test_relative_data_directory_is_rejected(monkeypatch):
    monkeypatch.setenv("WEBAGENT_DATA_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        Settings.from_env()


def test_inherited_v2_tracing_is_overridden(monkeypatch):
    for name in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_HANDLER"):
        monkeypatch.setenv(name, "true")
    disable_external_tracing()
    from langsmith.utils import tracing_is_enabled
    assert tracing_is_enabled() is False


def test_worker_runs_without_api_and_saver_owns_graph_tables(tmp_path):
    env = {**os.environ, "WEBAGENT_DATA_DIR": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "backend")}
    result = subprocess.run([sys.executable, "-m", "webagent", "worker", "--once"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"event": "worker_ready"' in result.stdout
    assert '"event": "worker_stopped"' in result.stdout
    with sqlite3.connect(tmp_path / "graph.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "checkpoints" in tables
    with sqlite3.connect(tmp_path / "business.sqlite3") as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
