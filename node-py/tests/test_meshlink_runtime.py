"""meshlink availability is a named, bounded condition — not a 500 per announce.

Runs without a meshlink checkout, a built extension or a GPU: the unavailable
case is the one under test (field case: hostd on xc-tower-ubuntu, ~1,200
`POST /peers` 500s per hour with ModuleNotFoundError mesh_route_py)."""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from livestack_node import hostd, meshlink_runtime as mr, mesh_peer
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device


@pytest.fixture
def no_meshlink(monkeypatch, tmp_path):
    """A process where the meshlink packages cannot be imported."""
    for name in ("mesh_route_py", "mesh_outbound_py"):
        monkeypatch.setitem(sys.modules, name, None)  # None => ImportError
    monkeypatch.delenv(mr.DIR_ENV, raising=False)
    monkeypatch.setattr(mesh_peer, "mesh_route_py", None)
    return tmp_path


def test_probe_names_what_is_missing(no_meshlink):
    st = mr.probe({})
    assert st["available"] is False
    assert "mesh_route_py" in st["missing"]
    assert "compiled extension" in st["reason"]


def test_invalid_dir_setting_is_named_not_ignored(no_meshlink):
    st = mr.probe({mr.DIR_ENV: str(no_meshlink / "nope")})
    assert st["available"] is False
    assert "is not a directory" in st["reason"]
    (no_meshlink / "empty").mkdir()
    st = mr.probe({mr.DIR_ENV: str(no_meshlink / "empty")})
    assert "not a meshlink checkout" in st["reason"]


def test_startup_check_logs_once_and_required_stops_the_process(no_meshlink):
    lines = []
    mr.startup_check(lines.append, env={})
    assert len(lines) == 1 and "UNAVAILABLE" in lines[0]
    with pytest.raises(SystemExit) as e:
        mr.startup_check(lines.append, env={mr.REQUIRED_ENV: "1"})
    assert "mesh_route_py" in str(e.value)


def test_meshpeer_construction_raises_the_named_error(no_meshlink):
    with pytest.raises(mr.MeshlinkUnavailable) as e:
        mesh_peer._import_meshlink()
    assert e.value.degradation == "meshlink_unavailable"
    assert isinstance(e.value, RuntimeError)


def test_rate_limited_log_suppresses_and_counts():
    t, out = [0.0], []
    log = mr.RateLimitedLog(out.append, window_s=60, clock=lambda: t[0])
    assert log("k", "a") and not log("k", "a") and not log("k", "a")
    assert log("other", "b")
    t[0] = 61
    assert log("k", "a")
    assert out == ["a", "b", "a (+2 suppressed in the last window)"]


def test_post_peers_mesh_announce_is_503_named_and_logged_once(no_meshlink, capsys):
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=lambda: 0.0)
    client = TestClient(hostd.build_app(broker), raise_server_exceptions=False)
    body = {"facade_url": "mesh://livestack/tower-asr/livestack", "host_id": "h"}
    codes = [client.post("/peers", json=body) for _ in range(5)]
    assert {r.status_code for r in codes} == {503}
    detail = codes[0].json()["detail"]
    assert detail["degradation"] == "meshlink_unavailable"
    assert "mesh_route_py" in detail["missing"]
    out = capsys.readouterr().out
    assert out.count("POST /peers refused") == 1          # rate-limited
    assert "Traceback" not in out
    assert broker.peers == []                              # no half-built row
    assert client.get("/status").json()["meshlink"]["available"] is False


def test_post_peers_mesh_announce_without_relay_keys_is_503_named(monkeypatch, capsys):
    """meshlink imports, but this hostd has no cap key ring: xc-tower-ubuntu
    2026-11-03 answered each mesh:// announce with a ValueError 500 (~10/min)."""
    monkeypatch.setattr(mesh_peer, "_MESHLINK_IMPORT_ERROR", None)
    monkeypatch.setattr(mesh_peer, "mesh_route_py", object())
    monkeypatch.delenv("LIVESTACK_RELAY_CAP_KEYS", raising=False)
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=lambda: 0.0)
    client = TestClient(hostd.build_app(broker), raise_server_exceptions=False)
    body = {"facade_url": "mesh://livestack/tower-asr/livestack", "host_id": "h"}
    codes = [client.post("/peers", json=body) for _ in range(3)]
    assert {r.status_code for r in codes} == {503}
    detail = codes[0].json()["detail"]
    assert detail["degradation"] == "mesh_relay_unconfigured"
    assert detail["missing"] == ["LIVESTACK_RELAY_CAP_KEYS"]
    out = capsys.readouterr().out
    assert out.count("POST /peers refused") == 1
    assert "Traceback" not in out
    assert broker.peers == [] and broker.membership_snapshot() == []
