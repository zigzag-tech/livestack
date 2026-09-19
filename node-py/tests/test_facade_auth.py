"""The node facade's levers grow a principal: /lease, /model/warm|evict|reclaim
require a node credential (LIVESTACK_NODE_TOKENS_FILE, mode 0600); /residence,
/capability, /health stay open so a consumer can discover a node it cannot yet
authenticate to.

The requirement's third scenario lives here: an unauthenticated eviction is
refused — 401, and NOTHING is evicted. The refusal must not have a side effect
the caller could read as partial success.
"""
import json
import os

import pytest

pytest.importorskip("livestack_node")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node import attach  # noqa: E402
from livestack_node import ManagedUnit, ResidencyPolicy, noop_free  # noqa: E402

NODE_TOKEN = "n" * 40


def _write_tokens(tmp_path):
    f = tmp_path / "node-tokens.json"
    f.write_text(json.dumps(
        {NODE_TOKEN: {"name": "fleet-ops", "owner": "ops"}}))
    os.chmod(f, 0o600)
    return f


def make_app():
    units = {
        n: ManagedUnit(n, loader=(lambda n=n: f"m:{n}"), freer=noop_free,
                       residency_policy=(ResidencyPolicy.HARD_PIN if n == "asr"
                                         else ResidencyPolicy.UNPINNED))
        for n in ("asr", "diarize")
    }
    app = FastAPI()
    manager, _coord = attach(app, host_id="h", kind="polyasr", units=units,
                             idle_seconds=120, coload=True,
                             gpu_call=lambda fn: fn())
    return app, manager


@pytest.fixture()
def authed_app(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_NODE_TOKENS_FILE", str(_write_tokens(tmp_path)))
    return make_app()


@pytest.fixture()
def open_app(monkeypatch):
    monkeypatch.delenv("LIVESTACK_NODE_TOKENS_FILE", raising=False)
    monkeypatch.delenv("LIVESTACK_NODE_TOKENS", raising=False)
    return make_app()


AUTH = {"Authorization": f"Bearer {NODE_TOKEN}"}


# -- the requirement's scenario: unauthenticated eviction ---------------------

def test_unauthenticated_evict_is_401_and_nothing_evicted(authed_app):
    app, manager = authed_app
    client = TestClient(app)
    # A resident unit, warmed with a valid credential...
    r = client.post("/livestack/model/warm", json={"unit": "diarize"},
                    headers=AUTH)
    assert r.status_code == 200
    assert "diarize" in manager.resident
    # ...is NOT evicted by a caller with no credential. 401, state untouched.
    r = client.post("/livestack/model/evict", json={"unit": "diarize"})
    assert r.status_code == 401
    assert "diarize" in manager.resident
    # A wrong token is the same answer — not a weaker one.
    r = client.post("/livestack/model/evict", json={"unit": "diarize"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert "diarize" in manager.resident
    # And the holder of the token can.
    r = client.post("/livestack/model/evict", json={"unit": "diarize"},
                    headers=AUTH)
    assert r.status_code == 200
    assert "diarize" not in manager.resident


# -- the other levers ----------------------------------------------------------

def test_lease_requires_a_node_principal(authed_app):
    app, manager = authed_app
    client = TestClient(app)
    r = client.post("/livestack/lease", json={"kind": "diarize", "owner_id": "o"})
    assert r.status_code == 401
    assert "diarize" not in manager.resident

    r = client.post("/livestack/lease", json={"kind": "diarize", "owner_id": "o"},
                    headers=AUTH)
    assert r.status_code == 200
    assert "diarize" in manager.resident  # warmed on acquire, as today


def test_warm_and_reclaim_require_a_node_principal(authed_app):
    app, _ = authed_app
    client = TestClient(app)
    assert client.post("/livestack/model/warm",
                       json={"unit": "diarize"}).status_code == 401
    assert client.post("/livestack/model/reclaim",
                       json={}).status_code == 401
    assert client.post("/livestack/model/warm",
                       json={"unit": "diarize"},
                       headers=AUTH).status_code == 200
    assert client.post("/livestack/model/reclaim",
                       json={}, headers=AUTH).status_code == 200


def test_the_read_endpoints_stay_open(authed_app):
    """Discovery must not require a credential: a consumer reads /residence,
    /capability and /health to decide what this node IS before it has any
    right to pull its levers."""
    app, _ = authed_app
    client = TestClient(app)
    assert client.get("/livestack/health").status_code == 200
    cap = client.get("/livestack/capability")
    assert cap.status_code == 200
    assert cap.json()["kind"] == "polyasr"
    res = client.get("/livestack/residence")
    assert res.status_code == 200
    assert {u["kind"] for u in res.json()["units"]} == {"asr", "diarize"}


# -- no tokens configured: byte-for-byte today's behaviour ---------------------

def test_with_no_node_tokens_nothing_changes(open_app):
    """The deploy that sets no token file keeps the open surface it has today;
    existing callers (the host broker's dispatch loop among them) must not
    need to change the day this lands."""
    app, manager = open_app
    client = TestClient(app)
    r = client.post("/livestack/lease", json={"kind": "diarize", "owner_id": "o"})
    assert r.status_code == 200
    assert "diarize" in manager.resident
    assert client.post("/livestack/model/evict",
                       json={"unit": "diarize"}).status_code == 200
    assert "diarize" not in manager.resident
    assert client.post("/livestack/model/reclaim", json={}).status_code == 200
    assert client.get("/livestack/residence").status_code == 200


def test_a_world_readable_node_token_file_fails_closed(tmp_path, monkeypatch):
    """Same rule as the fleet table: a disclosed credential is refused, so
    every lever 401s until the operator chmods — never silently open."""
    f = _write_tokens(tmp_path)
    os.chmod(f, 0o644)
    monkeypatch.setenv("LIVESTACK_NODE_TOKENS_FILE", str(f))
    app, _ = make_app()
    client = TestClient(app)
    assert client.post("/livestack/model/evict",
                       json={"unit": "diarize"}).status_code == 401
    assert client.post("/livestack/model/evict",
                       json={"unit": "diarize"},
                       headers=AUTH).status_code == 401
