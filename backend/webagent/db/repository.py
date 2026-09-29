"""Small persistence helpers; callers own the short transaction.

These helpers store already-validated business input. Full task compilation,
permission validation, state transitions and HTTP idempotency belong to later
services. No API or model may submit arbitrary SQL through this module.
"""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3


def utc_text(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace('+00:00', 'Z')


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def writing(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise ValueError("Writes require an explicit transaction() block")


def create_task(db, *, task_id: str, instruction: str, requested_fields: list[str]) -> None:
    writing(db)
    if not requested_fields or not all(isinstance(x, str) and x for x in requested_fields):
        raise ValueError("A new task needs explicit missing fields")
    db.execute("""INSERT INTO tasks(task_id,original_instruction,requested_fields_json,created_at)
                  VALUES (?,?,?,?)""", (task_id, instruction, canonical_json(requested_fields), utc_text()))


def add_contract(db, content: dict) -> str:
    writing(db)
    if content.get('schema_version') != 'm0-contract-v1':
        raise ValueError("Unsupported contract schema version")
    version = content.get('contract_version')
    if type(version) is not int or version < 1:
        raise ValueError("contract_version must be a positive integer")
    payload = canonical_json(content)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    db.execute("""INSERT INTO contracts(task_id,contract_version,schema_version,scenario,
                  contract_sha256,content_json,created_at) VALUES (?,?,?,?,?,?,?)""",
               (content['task_id'], version, content['schema_version'], content['scenario'],
                digest, payload, utc_text()))
    return digest


def create_run(db, *, run_id: str, task_id: str, contract_version: int,
               graph_version: str, graph_state_schema_version: str,
               model_config_sha256: str, runtime_config_sha256: str,
               parent_run_id: str | None = None) -> None:
    writing(db)
    contract = db.execute("SELECT contract_sha256 FROM contracts WHERE task_id=? AND contract_version=?",
                          (task_id, contract_version)).fetchone()
    if contract is None:
        raise ValueError("Contract not found")
    db.execute("""INSERT INTO runs(run_id,task_id,contract_version,contract_sha256,parent_run_id,
                  thread_id,graph_version,graph_state_schema_version,model_config_sha256,
                  runtime_config_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (run_id, task_id, contract_version, contract[0], parent_run_id, run_id,
                graph_version, graph_state_schema_version, model_config_sha256,
                runtime_config_sha256, utc_text()))


def get_contract(db, task_id: str, version: int) -> dict | None:
    row = db.execute("SELECT content_json FROM contracts WHERE task_id=? AND contract_version=?",
                     (task_id, version)).fetchone()
    return None if row is None else json.loads(row[0])


def list_runs(db, task_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM runs WHERE task_id=? ORDER BY created_at,run_id", (task_id,))]
