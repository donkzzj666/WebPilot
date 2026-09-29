"""The IPv6 route-probe classification must never hide real network activity."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "m1_probe", Path(__file__).resolve().parents[2] / "scripts" / "verification" / "verify_m1_01.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def netlog():
    return {
        "constants": {
            "logEventTypes": {"SOCKET_ALIVE": 1, "UDP_CONNECT": 2, "UDP_LOCAL_ADDRESS": 3,
                              "HOST_RESOLVER_MANAGER_IPV6_REACHABILITY_CHECK": 4,
                              "UDP_BYTES_SENT": 5, "HOST_RESOLVER_MANAGER_REQUEST": 6},
            "logSourceType": {"UDP_SOCKET": 24, "UDP_CLIENT_SOCKET": 49},
        },
        "events": [
            {"type": 1, "phase": 1, "source": {"id": 20, "type": 49}, "params": {"source_dependency": {"id": 19, "type": 7}}},
            {"type": 1, "phase": 1, "source": {"id": 21, "type": 24}, "params": {"source_dependency": {"id": 20, "type": 49}}},
            {"type": 2, "phase": 1, "source": {"id": 21, "type": 24}, "params": {"address": "[2001:4860:4860::8888]:443"}},
            {"type": 3, "phase": 0, "source": {"id": 21, "type": 24}},
            {"type": 1, "phase": 2, "source": {"id": 21, "type": 24}},
            {"type": 4, "phase": 0, "source": {"id": 19, "type": 7}, "params": {"cached": False}},
        ],
    }


@pytest.mark.parametrize("fault", [None, "sends_bytes", "not_closed", "unrelated_probe", "external_dns", "sendto_without_connect", "unattributed_udp"])
def test_route_probe_is_narrowly_classified(tmp_path, fault):
    data = netlog()
    if fault == "sends_bytes":
        data["events"].append({"type": 5, "phase": 0, "source": {"id": 21, "type": 24}, "params": {"byte_count": 1}})
    elif fault == "not_closed":
        del data["events"][4]
    elif fault == "unrelated_probe":
        data["events"][-1]["source"]["id"] = 999
    elif fault == "external_dns":
        data["events"].append({"type": 6, "phase": 1, "source": {"id": 99, "type": 7}, "params": {"host": "https://example.com"}})
    elif fault in {"sendto_without_connect", "unattributed_udp"}:
        params = {"address": "8.8.8.8:53"} if fault == "sendto_without_connect" else {}
        data["events"].append({"type": 5, "phase": 0, "source": {"id": 99, "type": 24}, "params": params})
    path = tmp_path / "netlog.json"
    path.write_text(json.dumps(data))
    result = probe.chromium_audit(path)
    assert bool(result["non_loopback"]) is bool(fault)
    if fault is None:
        assert len(result["route_only_probes"]) == 1
