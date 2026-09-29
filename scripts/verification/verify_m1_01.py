#!/usr/bin/env python3
"""Exercise the real asynchronous framework/browser stack using only a local fixture.

This is an integration probe for M1-01, not the M1-16 product graph. Network
audits observe this process, the Playwright Node driver and Chromium; they do
not establish an OS-wide firewall or satisfy M1-12's network boundary tests.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import sys
import traceback
from typing import TypedDict
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
TRACE_FLAGS = (
    "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"
)
# Set before importing LangGraph or any other framework, even if a caller opted in.
for _flag in TRACE_FLAGS:
    os.environ[_flag] = "false"
os.environ["LANGCHAIN_HANDLER"] = "false"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache" / "ms-playwright"))

EXPECTED_PACKAGES = {
    "langgraph": "1.2.12",
    "langgraph-checkpoint-sqlite": "3.1.1",
    "playwright": "1.63.0",
}
FIXTURE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M1-01 local integration fixture</title>
<style>body{font:18px system-ui;margin:64px;color:#203044;background:#f6f8fb}
main{max-width:740px;padding:36px;background:white;border:1px solid #dce3ed;border-radius:12px}
dt{color:#64748b;margin-top:24px}dd{margin:6px 0;font-weight:600}small{color:#64748b}</style>
</head><body><main><small>WebAgent / M1-01 verification only</small>
<h1>Local browser fixture</h1><p>This synthetic page contains no external assets.</p>
<dl><dt>Reference</dt><dd data-testid="reference">M1-01-local-only</dd>
<dt>Amount</dt><dd data-testid="amount">42.50</dd>
<dt>Status</dt><dd data-testid="status">ready</dd></dl></main></body></html>"""


def dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def is_loopback(host: object) -> bool:
    value = str(host).lower().rstrip(".")
    if value == "localhost":
        return True
    if "://" in value:
        value = urlsplit(value).hostname or ""
    elif value.startswith("["):
        value = value[1:].split("]", 1)[0]
    else:
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            if value.count(":") == 1:
                value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


class PythonNetworkAudit:
    def __init__(self, output: Path):
        self.events: list[dict] = []
        self.output = output.open("a", encoding="utf-8", buffering=1)

    def __call__(self, event: str, args: tuple) -> None:
        record = None
        if event in {"socket.connect", "socket.sendto"}:
            sock = args[0]
            address = args[1] if event == "socket.connect" else args[-1]
            record = {"event": event, "family": int(sock.family), "address": address}
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                record["loopback"] = is_loopback(address[0])
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            record = {"event": event, "host": args[0], "loopback": is_loopback(args[0])}
        if record:
            record["at"] = datetime.now(timezone.utc).isoformat()
            self.events.append(record)
            self.output.write(json.dumps(record, default=str) + "\n")


def sqlite_probe(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as db, db:
        journal_mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        db.execute("CREATE VIRTUAL TABLE temp.m1_fts_probe USING fts5(content)")
        db.execute("INSERT INTO m1_fts_probe VALUES ('repeatable local launch')")
        fts5 = db.execute("SELECT count(*) FROM m1_fts_probe WHERE m1_fts_probe MATCH 'repeatable'").fetchone()[0] == 1
        options = [row[0] for row in db.execute("PRAGMA compile_options")]
    version = sqlite3.sqlite_version_info
    # Official WAL-reset fixes: 3.51.3+, with 3.50.7 and 3.44.6 backports.
    wal_fix = version >= (3, 51, 3) or ((3, 50, 7) <= version < (3, 51, 0)) or ((3, 44, 6) <= version < (3, 45, 0))
    assert journal_mode == "wal" and fts5 and wal_fix, "SQLite WAL/FTS5 or WAL-reset fix requirement failed"
    return {"python_linked_version": sqlite3.sqlite_version, "journal_mode": journal_mode, "fts5_query_passed": fts5, "wal_fix_version_passed": wal_fix, "compile_options": options}


class ProbeState(TypedDict, total=False):
    url: str
    reference: str
    amount: str
    status: str
    observation_id: int
    verified: bool


async def integration(output: Path, headed: bool, report: dict) -> None:
    # Deliberately deferred until trace settings and the socket audit are installed.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from playwright.async_api import async_playwright

    business_path = output / "business.sqlite3"
    graph_path = output / "graph.sqlite3"
    report["sqlite"] = sqlite_probe(business_path)
    assert business_path.resolve() != graph_path.resolve()
    with closing(sqlite3.connect(business_path)) as db, db:
        db.execute("CREATE TABLE verification_observations (id INTEGER PRIMARY KEY, reference TEXT NOT NULL, amount TEXT NOT NULL, status TEXT NOT NULL)")

    requests: list[dict] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            line = raw.split(b"\r\n", 1)[0].decode("ascii", "replace")
            # This fixed-response test server also acts as a non-forwarding proxy.
            # Chrome background requests are recorded/rejected locally; no upstream
            # socket or CONNECT tunnel can be created by this fixture.
            target = line.split(" ")[1]
            allowed = line.startswith("GET ") and target in {
                "/fixture", "/audit-self-check", "/favicon.ico",
                f"http://127.0.0.1:{port}/fixture", f"http://127.0.0.1:{port}/favicon.ico",
            }
            requests.append({"request": line, "peer": writer.get_extra_info("peername"), "allowed": allowed, "upstream_forwarded": False})
            body = FIXTURE if allowed and target.endswith("/fixture") else b"ok" if allowed else b"M1-01 fixture: external access denied"
            status = b"200 OK" if allowed else b"403 Forbidden"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/fixture"
    try:
        # Known successful socket connect proves that the Python hook is active.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /audit-self-check HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        assert b"200 OK" in await reader.read()
        writer.close()
        await writer.wait_closed()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=not headed,
                proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                args=[f"--log-net-log={output / 'chromium-netlog.json'}", "--net-log-capture-mode=Everything", "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-first-run", "--host-resolver-rules=MAP * 127.0.0.1"],
            )
            report["browser"] = {"version": browser.version, "executable": playwright.chromium.executable_path, "headless": not headed}
            report["test_network_policy"] = {
                "mode": "fixed local fixture proxy; no upstream forwarding",
                "resolver": "test-only MAP * 127.0.0.1; proxy still rejects every non-fixture target",
                "allowed_url": url,
                "scope": "Only this M1-01 probe. Product browser networking is not implemented; M1-12 must implement and verify its own controls.",
            }
            browser_manifest = Path(__import__("playwright").__file__).parent / "driver" / "package" / "browsers.json"
            report["browser"]["playwright_manifest"] = json.loads(browser_manifest.read_text())
            try:
                context = await browser.new_context(viewport={"width": 1000, "height": 720}, service_workers="block")
                page = await context.new_page()

                async def observe(state: ProbeState) -> dict:
                    response = await page.goto(state["url"], wait_until="networkidle")
                    assert response and response.status == 200
                    fields = {name: await page.get_by_test_id(name).inner_text() for name in ("reference", "amount", "status")}
                    await page.screenshot(path=str(output / "local-fixture.png"), full_page=True)
                    with closing(sqlite3.connect(business_path)) as db, db:
                        cursor = db.execute("INSERT INTO verification_observations (reference,amount,status) VALUES (?,?,?)", (fields["reference"], fields["amount"], fields["status"]))
                        observation_id = cursor.lastrowid
                    return {**fields, "observation_id": observation_id}

                async def verify(state: ProbeState) -> dict:
                    # Deterministic fixture assertion only; this is not a model or product verifier.
                    assert (state["reference"], state["amount"], state["status"]) == ("M1-01-local-only", "42.50", "ready")
                    with closing(sqlite3.connect(business_path)) as db, db:
                        row = db.execute("SELECT reference,amount,status FROM verification_observations WHERE id=?", (state["observation_id"],)).fetchone()
                    assert row == (state["reference"], state["amount"], state["status"])
                    return {"verified": True}

                builder = StateGraph(ProbeState)
                builder.add_node("observe_fixture", observe)
                builder.add_node("assert_fixture", verify)
                builder.add_edge(START, "observe_fixture")
                builder.add_edge("observe_fixture", "assert_fixture")
                builder.add_edge("assert_fixture", END)
                config = {"configurable": {"thread_id": "m1-01-local-integration"}}
                async with AsyncSqliteSaver.from_conn_string(str(graph_path)) as saver:
                    graph = builder.compile(checkpointer=saver)
                    final = await graph.ainvoke({"url": url}, config=config, durability="sync")
                    history = [state async for state in graph.aget_state_history(config)]
                    assert final["verified"] and len(history) >= 4
                    checkpoint = await saver.aget_tuple(config)
                    assert checkpoint is not None
                # Reopen the SQLite connection after graph completion; no new-process recovery claim.
                async with AsyncSqliteSaver.from_conn_string(str(graph_path)) as reopened:
                    reloaded = await builder.compile(checkpointer=reopened).aget_state(config)
                    assert reloaded.values == final and not reloaded.next
                report["integration"] = {
                    "passed": True, "durability": "sync", "state": final,
                    "history_entries": len(history), "checkpoint_id": checkpoint.checkpoint["id"],
                    "checkpoint_reopened_and_read": True, "new_process_recovery_tested": False,
                    "business_database": business_path.name, "graph_database": graph_path.name,
                    "graph_version": "m1-01-probe-v1", "state_schema_version": 1,
                }
                with closing(sqlite3.connect(graph_path)) as db, db:
                    report["integration"]["graph_tables"] = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                await context.close()
            finally:
                await browser.close()
    finally:
        server.close()
        await server.wait_closed()
        dump(output / "fixture-http-requests.json", requests)


def chromium_audit(path: Path) -> dict:
    log = json.loads(path.read_text())
    names = {value: key for key, value in log["constants"]["logEventTypes"].items()}
    source_types = {value: key for key, value in log["constants"]["logSourceType"].items()}
    by_source: dict[int, list] = {}
    for event in log["events"]:
        by_source.setdefault(event["source"]["id"], []).append(event)
    connects, dns, route_only, udp_activity = [], [], [], []
    for event in log["events"]:
        name = names.get(event["type"], str(event["type"]))
        params = event.get("params", {})
        if name.startswith("UDP_") and ("BYTES_" in name or "ERROR" in name):
            # sendto() can transmit without UDP_CONNECT. Missing attribution is
            # a failure, not evidence that a datagram stayed local.
            related = by_source[event["source"]["id"]]
            destination = params.get("address") or params.get("remote_address") or next(
                (e.get("params", {}).get("address") for e in related
                 if names.get(e["type"]) == "UDP_CONNECT" and e.get("params", {}).get("address")), None
            )
            udp_activity.append({"event": name, "source_id": event["source"]["id"],
                                 "destination": destination, "loopback": bool(destination and is_loopback(destination))})
        if name in {"TCP_CONNECT_ATTEMPT", "UDP_CONNECT", "SOCKET_POOL_CONNECT_JOB_CONNECT", "QUIC_SESSION"}:
            for key in ("address", "remote_address", "peer_address"):
                if params.get(key):
                    item = {"event": name, "source_id": event["source"]["id"], "destination": params[key], "loopback": is_loopback(params[key])}
                    related = by_source[event["source"]["id"]]
                    parent = next((e.get("params", {}).get("source_dependency", {}) for e in related if names.get(e["type"]) == "SOCKET_ALIVE" and e["phase"] == 1), {})
                    ancestor_ids = {parent.get("id")}
                    for parent_event in by_source.get(parent.get("id"), []):
                        ancestor_ids.add(parent_event.get("params", {}).get("source_dependency", {}).get("id"))
                    # Chromium UDP connect()/getsockname() routing probe: no packet
                    # is sent. Require the exact source kind, endpoint, closed socket
                    # and an event whitelist; never exempt a UDP send or receive.
                    route_probe = (
                        name == "UDP_CONNECT" and params[key] == "[2001:4860:4860::8888]:443"
                        and source_types.get(parent.get("type")) == "UDP_CLIENT_SOCKET"
                        and any(names.get(e["type"]) == "SOCKET_ALIVE" and e["phase"] == 2 for e in related)
                        and all(names.get(e["type"]) in {"SOCKET_ALIVE", "UDP_CONNECT", "UDP_LOCAL_ADDRESS"} for e in related)
                        and any(names.get(e["type"]) == "HOST_RESOLVER_MANAGER_IPV6_REACHABILITY_CHECK"
                                and e["source"]["id"] in ancestor_ids
                                and e.get("params", {}).get("cached") is False
                                for e in log["events"])
                    )
                    if route_probe:
                        route_only.append({**item, "classification": "IPv6 routing capability check; closed without any UDP send/receive event"})
                    else:
                        connects.append(item)
        if ("HOST_RESOLVER" in name or "DNS_TRANSACTION" in name) and any(key in params for key in ("host", "hostname")):
            host = params.get("host", params.get("hostname"))
            dns.append({"event": name, "host": host, "loopback": is_loopback(host)})
    return {"event_count": len(log["events"]), "connection_attempts": connects, "resolver_requests": dns,
            "route_only_probes": route_only, "udp_activity": udp_activity,
            "non_loopback": [item for item in connects + dns + udp_activity if not item["loopback"]],
            "known_loopback_connect_observed": any(item["loopback"] for item in connects)}


def audit_summary(output: Path, python: PythonNetworkAudit) -> dict:
    node_path = output / "node-network-audit.jsonl"
    node_events = [json.loads(line) for line in node_path.read_text().splitlines()] if node_path.exists() else []
    node_violations = [item for item in node_events if (item["event"] == "net.connect" and item.get("transport") == "tcp" or item["event"].startswith("dns.")) and not is_loopback(item["host"])]
    # Unexpected UDP needs review rather than being silently declared local.
    node_violations.extend(item for item in node_events if item["event"].startswith("dgram."))
    chromium = chromium_audit(output / "chromium-netlog.json")
    summary = {
        "python": {"events": len(python.events), "non_loopback": [item for item in python.events if item.get("loopback") is False], "known_loopback_connect_observed": any(item["event"] == "socket.connect" and item.get("loopback") for item in python.events)},
        "node_driver": {"events": len(node_events), "audit_loaded": any(item["event"] == "audit_loaded" for item in node_events), "audit_exit": any(item["event"] == "audit_exit" for item in node_events), "non_loopback_or_unclassified_udp": node_violations},
        "chromium": chromium,
        "scope": "Python socket audit + Node net/dns/dgram hooks + Chromium netlog for this synthetic fixture run; attempted connections and resolver requests, not OS-wide packet capture or a network enforcement boundary. The Node driver normally uses stdio pipes and may have no network events. Child/native networking outside these observed APIs is not claimed as covered.",
    }
    summary["passed"] = bool(summary["python"]["known_loopback_connect_observed"] and not summary["python"]["non_loopback"] and summary["node_driver"]["audit_loaded"] and not node_violations and chromium["known_loopback_connect_observed"] and not chromium["non_loopback"])
    dump(output / "network-summary.json", summary)
    return summary


def main() -> int:
    if not __debug__:
        raise RuntimeError("Verification must not run with Python -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of the default headless browser")
    parser.add_argument("--output-dir", type=Path, help="New evidence directory; existing directories are refused")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = (args.output_dir or ROOT / "artifacts" / "verification" / "M1-01" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    python_audit = PythonNetworkAudit(output / "python-network-audit.jsonl")
    sys.addaudithook(python_audit)
    os.environ["NODE_OPTIONS"] = f'--require "{ROOT / "scripts" / "verification" / "network-audit.cjs"}"'
    os.environ["WEBAGENT_NETWORK_AUDIT"] = str(output / "node-network-audit.jsonl")
    report = {
        "task": "M1-01", "started_at": datetime.now(timezone.utc).isoformat(), "passed": False,
        "python": platform.python_version(), "platform": platform.platform(),
        "tracing": {flag: os.environ[flag] for flag in TRACE_FLAGS} | {"LANGCHAIN_HANDLER_present": "LANGCHAIN_HANDLER" in os.environ},
        "scope": "M1-01 real asynchronous dependency integration on a synthetic local page. No model, API key or real business website. M1-16 product graph, new-process recovery and M1-12 access boundary are not implemented or accepted by this probe.",
    }
    try:
        packages = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
        report["packages"] = dict(sorted(packages.items(), key=lambda item: item[0].lower()))
        for name, version in EXPECTED_PACKAGES.items():
            assert importlib.metadata.version(name) == version, f"{name} must be {version}"
        asyncio.run(integration(output, args.headed, report))
        report["network_audit"] = audit_summary(output, python_audit)
        assert report["network_audit"]["passed"], "Network audit failed; review network-summary.json and raw evidence"
        report["passed"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        if "network_audit" not in report and (output / "chromium-netlog.json").exists():
            try:
                report["network_audit"] = audit_summary(output, python_audit)
            except Exception as audit_error:
                report["network_audit_error"] = str(audit_error)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["artifact_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output.iterdir()) if path.is_file() and path.name != "report.json"}
        dump(output / "report.json", report)
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json"), "error": report.get("error", {}).get("message")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
