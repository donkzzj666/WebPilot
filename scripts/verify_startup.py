#!/usr/bin/env python3
"""Verify independent M1-01 processes and repeated startup using isolated local data.

Uses only this checkout's dev entry point and bundled Playwright Chromium. This
probe checks process availability, storage initialization and the health UI; it
does not execute a task or accept the later scheduling/recovery work packages.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
TRACE_FLAGS = (
    "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2", "LANGCHAIN_HANDLER",
)
for flag in TRACE_FLAGS:
    os.environ[flag] = "false"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".cache" / "ms-playwright")
HTTP = build_opener(ProxyHandler({}))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request(url: str) -> tuple[int, bytes]:
    try:
        with HTTP.open(url, timeout=2) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except (URLError, TimeoutError, ConnectionError):
        return 0, b""


def free_ports() -> tuple[int, int]:
    with socket.socket() as api, socket.socket() as ui:
        api.bind(("127.0.0.1", 0))
        ui.bind(("127.0.0.1", 0))
        return api.getsockname()[1], ui.getsockname()[1]


class Process:
    def __init__(self, name: str, component: str, output: Path, env: dict[str, str]):
        self.name = name
        self.log_path = output / f"{name}.log"
        self.log = self.log_path.open("w", encoding="utf-8", buffering=1)
        self.started_at = now()
        self.forced_kill = False
        self.proc = subprocess.Popen(
            [str(ROOT / "scripts" / "dev.sh"), component], cwd=ROOT, env=env,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def assert_alive(self) -> None:
        if not self.alive():
            raise AssertionError(f"{self.name} exited {self.proc.returncode}; see {self.log_path.name}")

    def contains(self, text: str) -> bool:
        return text in self.log_path.read_text(encoding="utf-8")

    def stop(self) -> dict:
        # Each process owns a new session. Never signal pre-existing processes.
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.forced_kill = True
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        self.log.close()
        return {
            "name": self.name, "pid": self.proc.pid, "started_at": self.started_at,
            "exit_code": self.proc.returncode, "forced_kill": self.forced_kill,
            "log": self.log_path.name,
        }


def wait_for(check: Callable[[], bool], label: str, processes: list[Process]) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        for process in processes:
            process.assert_alive()
        if check():
            return
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {label}")


def databases(data: Path) -> dict:
    result = {}
    for name in ("business", "graph"):
        path = data / f"{name}.sqlite3"
        assert path.is_file(), f"Missing {path.name}"
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            counts = {
                name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name, _sql in tables
            }
        assert journal == "wal" and integrity == "ok", f"{name} SQLite check failed"
        result[name] = {"journal_mode": journal, "integrity": integrity, "tables": tables, "row_counts": counts}
    assert not result["business"]["tables"], "M1-01 must not implement M1-02 business entities"
    assert "checkpoints" in result["graph"]["row_counts"], "Worker did not initialize its saver"
    assert (data / "business.sqlite3").stat().st_ino != (data / "graph.sqlite3").stat().st_ino
    return result


def verify(output: Path, report: dict) -> None:
    from playwright.sync_api import sync_playwright

    data = output / "data"
    data.mkdir()
    api_port, ui_port = free_ports()
    api_url = f"http://127.0.0.1:{api_port}"
    ui_url = f"http://127.0.0.1:{ui_port}"
    env = {
        **os.environ, "WEBAGENT_DATA_DIR": str(data),
        "WEBAGENT_API_PORT": str(api_port), "WEBAGENT_UI_PORT": str(ui_port),
        "PYTHONUNBUFFERED": "1", "NO_COLOR": "1",
    }
    # Do not inherit arbitrary Node hooks or proxy destinations into the probe.
    for name in ("NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    report["configuration"] = {
        "data_dir": str(data), "api_url": api_url, "ui_url": ui_url,
        "tracing": {flag: env[flag] for flag in TRACE_FLAGS},
    }
    active: list[Process] = []
    report["processes"] = []
    report["checks"] = []
    requests: list[dict] = []
    errors: list[str] = []

    def record(name: str, **details: object) -> None:
        report["checks"].append({"name": name, "passed": True, "at": now(), **details})
        print(json.dumps({"check": name, "passed": True}), flush=True)

    def start(name: str, component: str) -> Process:
        process = Process(name, component, output, env)
        active.append(process)
        return process

    def stop(process: Process) -> None:
        result = process.stop()
        active.remove(process)
        report["processes"].append(result)
        assert not result["forced_kill"], f"{process.name} did not stop after SIGTERM"
        # Vite's signal handler exits with 128 + SIGTERM; Python may report -15.
        assert result["exit_code"] in (0, -signal.SIGTERM, 128 + signal.SIGTERM), f"Unexpected {process.name} exit"

    def healthy() -> bool:
        status, body = request(api_url + "/health")
        if status != 200:
            return False
        value = json.loads(body)
        return value.get("status") == "ok" and value.get("task_execution_enabled") is False

    try:
        frontend = start("first-frontend", "frontend")
        wait_for(lambda: request(ui_url)[0] == 200, "frontend", [frontend])
        assert request(api_url + "/health")[0] == 0
        assert not list(data.iterdir()), "Frontend must not initialize backend storage"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=[
                "--disable-background-networking", "--disable-component-update",
                "--disable-domain-reliability", "--disable-sync", "--no-first-run",
                "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication",
                "--metrics-recording-only", "--disable-default-apps",
                "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=127.0.0.1;localhost",
            ])
            report["chromium_version"] = browser.version
            context = browser.new_context(viewport={"width": 1280, "height": 960}, service_workers="block")

            def route_request(route) -> None:
                url = urlsplit(route.request.url)
                local = url.hostname == "127.0.0.1" and url.port in (api_port, ui_port) and url.scheme in ("http", "ws")
                requests.append({"url": route.request.url, "allowed": local})
                if local:
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ui_url, wait_until="networkidle")
            page.get_by_role("heading", name="API 未就绪", exact=True).wait_for()
            page.get_by_text("任务执行未开放", exact=True).wait_for()
            page.screenshot(path=str(output / "01-frontend-only.png"), full_page=True)
            record("frontend_starts_without_api", api_status=request(api_url + "/health")[0], screenshot="01-frontend-only.png")

            api = start("first-api", "api")
            wait_for(healthy, "API health", [api, frontend])
            assert not (data / "graph.sqlite3").exists(), "API must not own the graph database"
            page.get_by_role("button", name="重新检查连接", exact=True).click()
            page.get_by_role("heading", name="API 已连接", exact=True).wait_for()
            page.screenshot(path=str(output / "02-api-connected.png"), full_page=True)
            record("api_health_visible_in_browser", health=json.loads(request(api_url + "/health")[1]), screenshot="02-api-connected.png")

            worker = start("first-worker", "worker")
            wait_for(lambda: worker.contains('"event": "worker_ready"'), "Worker readiness", [worker, api, frontend])
            before = databases(data)
            record("independent_worker_initializes_two_databases", storage=before)

            stop(api)
            worker.assert_alive()
            frontend.assert_alive()
            assert request(api_url + "/health")[0] == 0
            assert request(ui_url)[0] == 200
            page.get_by_role("button", name="重新检查连接", exact=True).click()
            page.get_by_role("heading", name="API 未就绪", exact=True).wait_for()
            page.screenshot(path=str(output / "03-api-stopped.png"), full_page=True)
            record("worker_and_frontend_survive_api_shutdown", screenshot="03-api-stopped.png")

            stop(worker)
            assert worker.contains('"event": "worker_stopped"')
            stop(frontend)
            assert request(ui_url)[0] == 0
            record("first_cycle_sigterm_cleanup")

            # Start the Worker first to prove it has no API startup dependency.
            worker = start("restart-worker", "worker")
            wait_for(lambda: worker.contains('"event": "worker_ready"'), "Worker-only restart", [worker])
            assert request(api_url + "/health")[0] == 0
            record("worker_starts_without_api")
            api = start("restart-api", "api")
            frontend = start("restart-frontend", "frontend")
            wait_for(healthy, "restarted API", [worker, api, frontend])
            wait_for(lambda: request(ui_url)[0] == 200, "restarted frontend", [worker, api, frontend])
            page.reload(wait_until="networkidle")
            page.get_by_role("heading", name="API 已连接", exact=True).wait_for()
            after = databases(data)
            assert before == after, "Repeated startup changed database schema or records"
            page.screenshot(path=str(output / "04-restarted.png"), full_page=True)
            record("same_directory_restart_is_idempotent", storage=after, screenshot="04-restarted.png")
            context.close()
            browser.close()

        stop(api)
        stop(worker)
        stop(frontend)
        assert worker.contains('"event": "worker_stopped"')
        assert request(api_url + "/health")[0] == 0 and request(ui_url)[0] == 0
        record("second_cycle_sigterm_cleanup")
        assert not errors, f"Browser JavaScript errors: {errors}"
        assert not [item for item in requests if not item["allowed"]], "Page attempted a non-local request"
        report["passed"] = True
    finally:
        cleanup_errors = []
        for process in reversed(active):
            try:
                report["processes"].append(process.stop())
            except Exception as error:
                cleanup_errors.append(f"{process.name}: {error}")
        report["browser_requests"] = requests
        report["browser_page_errors"] = errors
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["passed"] = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="New evidence directory; existing directories are refused")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("startup-%Y%m%dT%H%M%S.%fZ")
    output = (args.output_dir or ROOT / "artifacts" / "verification" / "M1-01" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "task": "M1-01", "probe": "independent-process-startup", "started_at": now(), "passed": False,
        "scope": "Isolated local process startup/restart, health UI and empty business/separate graph SQLite initialization. No model, credentials, business task, persistent scheduler, or crash-recovery acceptance.",
    }
    try:
        verify(output, report)
    except Exception as error:
        report["passed"] = False
        report["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    finally:
        report["finished_at"] = now()
        report["artifact_sha256"] = {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "report.json"
        }
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json"), "error": report.get("error", {}).get("message")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
