"""Owner through the engine: harmony-llm reads X-Harmony-Owner and admits as
the asserted owner; the ledger ends up with one resident unit and two Grant
records under two different owners.

Requirement (umbrella, verified here in-process):
    Two apps, one unit, two owners in the ledger: benchday (`benchday:acct_b`)
    and attune (`attune:acct_a`) each send one request to the same LLM unit →
    two Grant records with two different owners and one resident unit.

The engine module is imported from its path with a controlled environment:
warm-on-start off (it would spawn vLLM), registration off (no announce
threads), and a fixed HOST_ID so the engine identity is deterministic.
"""
import importlib.util
import io
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_SERVER_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "examples", "harmony-llm", "server.py"))


def _load_server(monkeypatch):
    monkeypatch.setenv("HARMONY_LLM_HOST_ID", "test-node")
    monkeypatch.setenv("HARMONY_LLM_WARM_ON_START", "0")
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_ACT_SAMPLE_S", "0")
    spec = importlib.util.spec_from_file_location(
        "harmony_llm_server_under_test", _SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeAdmit:
    """Records the identity arguments and declines, so the proxy raises its
    503 without touching a model loader."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind="", *, requires=None, owner_id="node",
                 token=None, owner_asserted=False, timeout=240.0,
                 brokers=None):
        self.calls.append({"kind": kind, "requires": requires,
                           "owner_id": owner_id, "token": token,
                           "owner_asserted": owner_asserted})
        return {"granted": False, "degraded": "fake broker unavailable"}


def test_the_hub_asserted_owner_is_admitted_as_owner(monkeypatch):
    mod = _load_server(monkeypatch)
    fake = _FakeAdmit()
    monkeypatch.setattr(mod, "admit", fake)
    client = TestClient(mod.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm"},
                    headers={"X-Harmony-Owner": "benchday:acct_b"})
    assert r.status_code == 503           # the fake declined; the call happened
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["owner_id"] == "benchday:acct_b"
    assert call["owner_asserted"] is True
    assert call["token"] is None          # HARMONY_LLM_FLEET_TOKEN unset here


def test_without_the_header_the_engine_charges_its_own_identity(monkeypatch):
    mod = _load_server(monkeypatch)
    fake = _FakeAdmit()
    monkeypatch.setattr(mod, "admit", fake)
    client = TestClient(mod.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm"})
    assert r.status_code == 503
    call = fake.calls[0]
    assert call["owner_id"] == "harmony-llm:test-node"
    assert call["owner_asserted"] is False


def test_two_apps_one_unit_two_owners_in_the_ledger(tmp_path, monkeypatch):
    """The full chain, in-process: two engine-shaped admissions (owner +
    owner_asserted, as client.admit sends them) planned together against a
    real broker + real planner + real ledger. Two Grant records name the two
    owners; ONE load — the second request is served by the copy the first
    brought in, which is the whole point of placing residency, not requests.
    """
    from livestack_node.hostbroker import HostBroker
    from livestack_node.ledger import JsonlLedger
    from livestack_node.planner import Device, Placement, Request, Unit

    GB = 1_000_000_000

    class _Peer:
        host_id = "h"
        device_id = "h/gpu0"

        def __init__(self):
            self._unit = Unit("llm", {"vram_bytes": 20 * GB}, priority=20)
            self._resident = False

        def units(self):
            return {"llm": self._unit}

        def placements(self):
            return ([Placement("llm", "h/gpu0", loaded_at=0)]
                    if self._resident else [])

        def warm(self, kind, device=None, budget=None):
            self._resident = True

        def evict(self, kind):
            self._resident = False

    peer = _Peer()
    led = JsonlLedger(str(tmp_path / "decisions.jsonl"))
    broker = HostBroker(
        [Device("h/gpu0", "h", capacity={"vram_bytes": 44 * GB},
                reserved={"vram_bytes": 2 * GB})],
        [peer], clock=lambda: 1000.0, ledger=led)
    broker.fleet_principals = None  # auth off: owners come from the body

    # As Harmony's /admit received them from the engine path (B.1): the owner
    # resolved from the engine's delegating credential, owner_asserted set by
    # the engine because its hub vouched the owner.
    broker.plan_and_apply([
        Request(id="req-b", kind="llm", owner="benchday:acct_b",
                owner_asserted=True),
        Request(id="req-a", kind="llm", owner="attune:acct_a",
                owner_asserted=True),
    ], {})

    records = led.read()
    grants = sorted((r for r in records if r["decision"] == "grant"),
                    key=lambda r: r["request"]["request_id"])
    assert len(grants) == 2, "one grant per admission, both ledgered"
    owners = {g["request"]["owner"] for g in grants}
    assert owners == {"benchday:acct_b", "attune:acct_a"}
    assert all(g["request"]["owner_asserted"] is True for g in grants)

    loads = [r for r in records if r["decision"] == "load" and r["kind"] == "llm"]
    assert len(loads) == 1, "one resident unit: the second grant reused it"
