"""M1-05 real loopback HTTP acceptance with synthetic model data only."""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.db.repository import canonical_json, create_run, utc_text
from webagent.evidence.service import EvidenceService
from webagent.models.adapter import ModelAdapter, ModelError
from webagent.models.journal import list_attempts
from webagent.models.schema import ModelInput
from webagent.models.transport import DeepSeekTransport, ModelConfig
from webagent.state import transition
from webagent.tasks.models import CreateTaskRequest
from webagent.tasks.service import create

SYNTHETIC_KEY = "synthetic-fixture-credential-no-provider-access"
PRIVATE_SENTINEL = "synthetic-private-reasoning-never-persist"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQVDIGAACuAGcVHqFfAAAAAElFTkSuQmCC")
FINANCE = {
    "instruction": "Read a synthetic local finance fixture", "source_ids": ["local-fixture"],
    "scenario": "finance", "parameters": {
        "entity_id": "fixture-company", "report_version": "2025", "period_type": "annual",
        "metrics": ["revenue"], "currency": "USD",
    },
}


def seed_input(database: Path, scenario: str, config: ModelConfig, *, image: bool = False) -> ModelInput:
    reply = create(database, CreateTaskRequest.model_validate(FINANCE), "model-probe-" + scenario)
    contract = reply.body["contract"]
    run_id, budget_id = "probe-" + scenario, "budget-" + scenario
    with connect(database) as db, transaction(db):
        create_run(db, run_id=run_id, task_id=contract["task_id"], contract_version=1,
                   graph_version="model-http-probe-v1", graph_state_schema_version="fixture-v1",
                   model_config_sha256=config.config_sha256, runtime_config_sha256="b" * 64)
        db.execute("INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)", (budget_id, run_id))
    transition(database, run_id=run_id, expected_state_version=0, target="RUNNING")
    now = utc_text()
    snapshot_id = "snapshot-" + run_id
    body = {
        "run_id": run_id, "contract": contract,
        "observation": {"snapshot_id": snapshot_id, "run_id": run_id, "captured_at": now,
                        "source_url": "http://127.0.0.1:8765/", "title": "Synthetic page",
                        "tab_id": "tab-1", "frame_id": "frame-1", "page_version": "page-1",
                        "width": 1, "height": 1, "visible_excerpt": "Synthetic revenue: 42 USD",
                        "evidence_ids": ["shot-1"], "redaction_status": "FILTERED"},
        "verified_checkpoint": {
            "checkpoint_id": "checkpoint-1", "task_id": contract["task_id"], "run_id": run_id,
            "contract_version": 1, "current_subgoal": "read-revenue", "verified_item_ids": [],
            "pending_item_ids": ["revenue"], "current_object_id": "fixture-company",
            "current_object_version": "2025", "current_snapshot_id": snapshot_id, "flow_version": None,
            "action_sequence": 0, "business_event_id": 0, "budget_record_ref": budget_id,
            "identity_ref": None, "pending_operation_ids": [], "epoch": 1,
            "evidence_ids": [], "saved_at": now,
        },
        "image_evidence_ids": ["shot-1"] if image else [],
        "allowed_action_schema_ref": "urn:webagent:m0-contract-v1:Action", "selected_flow_versions": [],
    }
    evidence = EvidenceService(database.parent)
    body["observation"] = evidence.publish_observation(body["observation"],
        {"title": "Synthetic page", "text": "Synthetic revenue: 42 USD",
         "screenshot": PNG if image else None}, mask_screenshot=image)
    if image:
        body["image_evidence_ids"] = [ref for ref in body["observation"]["evidence_ids"]
            if evidence.store.metadata(ref)["artifact_kind"] == "screenshot"]
    return ModelInput.model_validate_json(canonical_json(body), strict=True)


def candidate(model_input: dict, scenario: str) -> dict:
    if scenario == "action":
        observation = model_input["observation"]
        return {"type": "Action", "action": {
            "action_type": "read_visible", "run_id": model_input["run_id"], "step_id": "step-1",
            "epoch": 1, "snapshot_id": observation["snapshot_id"], "expected_effect": "read", "args": {},
            "target": {"page_url": observation["source_url"], "tab_id": observation["tab_id"],
                       "frame_id": observation["frame_id"], "locator": None, "write_scope": None},
        }}
    return {"type": "RequestInput", "requested_fields": ["report_page"],
            "reason": "The synthetic source does not identify a page"}


class FixtureServer:
    """Small HTTP/1.1 fixture: records safe summaries and observes socket EOF."""

    def __init__(self):
        self.requests = []
        self.counts = Counter()
        self.active = set()
        self.started = {name: asyncio.Event() for name in ("timeout", "cancel")}
        self.disconnected = {name: asyncio.Event() for name in ("timeout", "cancel")}
        self.errors = []
        self.expected_images = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.active.add(task)
        scenario = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            lines = header.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {key.lower(): value for key, value in headers.items()}
            length = int(headers["content-length"])
            assert 0 < length < 32 * 1024 * 1024
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), timeout=3))
            user = body["messages"][1]["content"]
            parts = user if isinstance(user, list) else []
            model_input = json.loads(parts[0]["text"] if parts else user)
            scenario = model_input["run_id"].removeprefix("probe-")
            self.counts[scenario] += 1
            image_parts = [part for part in parts if part.get("type") == "image_url"]
            prompt = body["messages"][0]["content"]
            summary = {
                "scenario": scenario, "attempt": self.counts[scenario], "method": method, "path": path,
                "synthetic_authorization_matched": headers.get("authorization") == "Bearer " + SYNTHETIC_KEY,
                "model": body["model"], "max_tokens": body["max_tokens"],
                "json_mode": body.get("response_format") == {"type": "json_object"},
                "thinking_disabled": body.get("thinking") == {"type": "disabled"},
                "stream_disabled": body.get("stream") is False, "tools_absent": "tools" not in body,
                "schema_present": "JSON Schema:" in prompt,
                "repair_diagnostics_present": "Correct the following validation fields" in prompt,
                "image_count": len(image_parts),
                "inline_image_verified": [part["image_url"]["url"] for part in image_parts] ==
                    self.expected_images.get(scenario, []),
            }
            self.requests.append(summary)
            assert method == "POST" and path == "/chat/completions"
            assert all(summary[key] for key in ("synthetic_authorization_matched", "json_mode",
                "thinking_disabled", "stream_disabled", "tools_absent", "schema_present", "inline_image_verified"))
            if scenario in ("timeout", "cancel"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\nConnection: close\r\n\r\n")
                await writer.drain()
                self.started[scenario].set()
                # Whitespace keeps arriving within the read timeout; only the
                # monotonic total deadline/caller cancellation should end this.
                while True:
                    writer.write(b" ")
                    await writer.drain()
                    try:
                        closed = await asyncio.wait_for(reader.read(1), timeout=.01)
                    except TimeoutError:
                        continue
                    if closed == b"":
                        self.disconnected[scenario].set()
                        return
            status = {"rate": 429, "auth": 401}.get(scenario, 200)
            if status != 200:
                payload = {"error": {"message": PRIVATE_SENTINEL}}
            else:
                raw = canonical_json(candidate(model_input, scenario))
                if scenario == "repair" and self.counts[scenario] == 1:
                    raw = "{malformed synthetic JSON"
                if scenario == "unknown":
                    raw = '{"type":"Action","action":{"action_type":"execute_shell"}}'
                payload = {"id": f"chatcmpl-{scenario}-{self.counts[scenario]}", "model": "deepseek-flash",
                           "system_fingerprint": "fp_fixture", "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": raw, "reasoning_content": PRIVATE_SENTINEL}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            response = canonical_json(payload).encode()
            response_headers = f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {len(response)}\r\nConnection: close\r\n"
            if status == 429:
                response_headers += "Retry-After: 2\r\n"
            writer.write(response_headers.encode() + b"\r\n" + response)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            if scenario in self.disconnected:
                self.disconnected[scenario].set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not expose request bodies or potentially echoed headers.
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            self.active.discard(task)

    async def settle(self):
        for _ in range(100):
            if not self.active:
                return
            await asyncio.sleep(.01)
        raise AssertionError("fixture still has an open request after cancellation")


async def verify(output: Path, report: dict):
    database = output / "business.sqlite3"
    storage = migrate(database)
    assert storage["schema_version"] == LATEST_VERSION
    fixture = FixtureServer()
    server = await asyncio.start_server(fixture.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    report["storage"] = storage
    all_attempts = {}

    def record(name, **values):
        report["checks"].append({"name": name, "passed": True, **values})

    try:
        for scenario in ("action", "image", "repair", "unknown", "rate", "auth", "timeout", "cancel"):
            config = ModelConfig(base_url=f"http://127.0.0.1:{port}", connect_seconds=.1,
                                 read_seconds=.1, total_seconds=.25 if scenario == "timeout" else 3.0)
            model_input = seed_input(database, scenario, config, image=scenario == "image")
            images = EvidenceService(database.parent).model_images(
                model_input.observation.snapshot_id, model_input.image_evidence_ids,
                run_id=model_input.run_id)
            fixture.expected_images[scenario] = ["data:" + item.mime_type + ";base64," +
                base64.b64encode(item.data).decode() for item in images]
            provider = DeepSeekTransport(config, SYNTHETIC_KEY, allow_test_loopback=True)
            adapter = ModelAdapter(database, provider)
            error = None
            try:
                if scenario == "cancel":
                    pending = asyncio.create_task(adapter.generate(model_input))
                    await asyncio.wait_for(fixture.started[scenario].wait(), timeout=2)
                    # A writer can commit while HTTP is in flight: no DB write
                    # lock spans the provider operation.
                    with connect(database, busy_timeout_ms=50) as db, transaction(db):
                        db.execute("SELECT COUNT(*) FROM model_attempts").fetchone()
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass
                    else:
                        raise AssertionError("caller cancellation was swallowed")
                    await asyncio.wait_for(fixture.disconnected[scenario].wait(), timeout=1)
                    record("caller_cancel_propagates_and_closes_http")
                else:
                    try:
                        result = await adapter.generate(model_input, images=images)
                    except ModelError as caught:
                        error = caught
                    if scenario in ("action", "image", "repair"):
                        assert error is None
                        assert result.output.type == ("Action" if scenario == "action" else "RequestInput")
                        record({"action": "real_http_json_action", "image": "text_inline_image_json_combination",
                                "repair": "malformed_json_repairs_once"}[scenario])
                    else:
                        expected = {"unknown": "invalid_output", "rate": "rate_limit",
                                    "auth": "invalid_credentials", "timeout": "timeout"}[scenario]
                        assert error is not None and error.error_class == expected
                        if scenario == "rate":
                            assert error.code == "MODEL_RATE_LIMIT" and error.retry_after_seconds == 2
                        elif scenario == "auth":
                            assert error.code == "UPSTREAM_ERROR" and error.status != 401
                        elif scenario == "timeout":
                            assert error.code == "TIMEOUT"
                            await asyncio.wait_for(fixture.disconnected[scenario].wait(), timeout=1)
                        record({"unknown": "unknown_action_stops_after_two_repairs", "rate": "model_429_no_retry",
                                "auth": "provider_401_not_local_auth", "timeout": "whitespace_total_deadline_closes_http"}[scenario],
                               error_class=error.error_class, code=error.code)
            finally:
                await provider.aclose()
            await fixture.settle()
            attempts = list_attempts(database, model_input.run_id)
            all_attempts[scenario] = attempts
            expected_count = {"repair": 2, "unknown": 3}.get(scenario, 1)
            assert fixture.counts[scenario] == expected_count == len(attempts)
            assert [item["attempt_number"] for item in attempts] == list(range(1, expected_count + 1))
            assert all(item["status"] != "STARTED" for item in attempts)
            expected_statuses = {"repair": ["INVALID", "VALID"], "unknown": ["INVALID"] * 3,
                                 "cancel": ["CANCELLED"], "rate": ["ERROR"], "auth": ["ERROR"],
                                 "timeout": ["ERROR"]}.get(scenario, ["VALID"])
            assert [item["status"] for item in attempts] == expected_statuses
            for index, item in enumerate(attempts):
                call = item["record"]
                assert call["format_repairs"] == index
                assert call["config_sha256"] == config.config_sha256
                assert call["estimated_cost"] is None and call["usage"]["image_units"] is None
                if scenario in ("rate", "auth", "timeout", "cancel"):
                    assert call["usage"]["input_tokens"] is None
                else:
                    assert call["usage"]["input_tokens"] == 10 and call["usage"]["output_tokens"] == 5
            with connect(database) as db:
                run = db.execute("SELECT state,state_version FROM runs WHERE run_id=?", (model_input.run_id,)).fetchone()
                assert tuple(run) == ("RUNNING", 1)
                budget = db.execute("SELECT model_calls_used FROM run_budgets WHERE run_id=?", (model_input.run_id,)).fetchone()
                assert budget[0] == expected_count
        assert not fixture.errors and not fixture.active
        assert fixture.requests[1]["image_count"] == 1
        repair_requests = [item for item in fixture.requests if item["scenario"] == "repair"]
        assert [item["repair_diagnostics_present"] for item in repair_requests] == [False, True]
        with connect(database) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            assert db.execute("SELECT COUNT(*) FROM model_attempts WHERE status='STARTED'").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 8
        record("all_attempts_durable_and_charged_to_original_run", attempts=sum(map(len, all_attempts.values())))
        record("run_state_events_unchanged_and_no_pending_requests", schema_version=LATEST_VERSION)
        persisted = canonical_json(all_attempts)
        assert SYNTHETIC_KEY not in persisted and PRIVATE_SENTINEL not in persisted
        assert b"synthetic-private-reasoning-never-persist" not in database.read_bytes()
        assert SYNTHETIC_KEY.encode() not in database.read_bytes()
        record("credentials_reasoning_and_raw_responses_not_persisted")
    finally:
        server.close()
        await server.wait_closed()
        for task in list(fixture.active):
            task.cancel()
        if fixture.active:
            await asyncio.gather(*fixture.active, return_exceptions=True)
        (output / "request-summary.json").write_text(json.dumps(fixture.requests, ensure_ascii=False, indent=2) + "\n")
        (output / "model-attempts.json").write_text(json.dumps(all_attempts, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = (args.output_dir or ROOT / "artifacts/verification/M1-05" / ("model-http-" + stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"task": "M1-05", "passed": False, "checks": [],
              "scope": "Real loopback HTTP with synthetic model responses and isolated SQLite. No live provider, credentials, paid calls, or browser execution."}
    try:
        asyncio.run(verify(output, report))
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    report["artifact_sha256"] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json"),
                      "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
