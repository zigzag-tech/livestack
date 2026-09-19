"""The ledger attributes loads and evictions: `request.caused_by` names the
owner whose request produced the action, and the field that carries a request
id is named `request_id` (renamed from the old, misleading `owner` key).

A reload is explained from the ledger alone: the evict names who needed the
room, the load names who brought the unit back — no engine journal consulted.

The broker is driven through FakePeers (the test_hostbroker pattern): a peer
reports its units and placements, and the broker dispatches warm/evict back to
it, so residency evolves across plan cycles exactly as it does against a live
node.
"""
from livestack_node.hostbroker import HostBroker
from livestack_node.ledger import JsonlLedger
from livestack_node.planner import Device, Placement, Request, Unit

GB = 1_000_000_000


class FakePeer:
    """One model-server process: reports its unit, flips residency when the
    broker dispatches warm/evict to it."""

    def __init__(self, device_id, unit, resident=False):
        self.host_id = "h"
        self.device_id = device_id
        self._unit = unit
        self._resident = resident
        self.calls = []

    def units(self):
        return {self._unit.kind: self._unit}

    def placements(self):
        if not self._resident:
            return []
        return [Placement(self._unit.kind, self.device_id, loaded_at=0)]

    def warm(self, kind, device=None, budget=None):
        self.calls.append(("warm", kind))
        self._resident = True

    def evict(self, kind):
        self.calls.append(("evict", kind))
        self._resident = False


def _broker(tmp_path):
    now = [1000.0]
    led = JsonlLedger(str(tmp_path / "decisions.jsonl"))
    llm = Unit("llm", {"vram_bytes": 35 * GB}, priority=30)
    big2 = Unit("big2", {"vram_bytes": 35 * GB}, priority=20)
    llm_peer = FakePeer("h/gpu0", llm)
    big2_peer = FakePeer("h/gpu0", big2)
    broker = HostBroker(
        [Device("h/gpu0", "h", capacity={"vram_bytes": 44 * GB},
                reserved={"vram_bytes": 2 * GB})],
        [llm_peer, big2_peer],
        clock=lambda: now[0],
        ledger=led)
    broker.fleet_principals = None  # auth off: owners come from the body
    return broker, led, now, llm_peer, big2_peer


def _reload_sequence(broker, now):
    broker.plan_and_apply([Request(id="r1", kind="llm",
                                   owner="attune:acct_a")], {})
    now[0] += 30                      # past the 15 s residency floor
    broker.plan_and_apply([Request(id="r2", kind="big2",
                                   owner="benchday:acct_b")], {})
    now[0] += 30
    broker.plan_and_apply([Request(id="r3", kind="llm", owner="attune:acct_a",
                                   priority=10)], {})


def test_one_evict_and_one_load_read_back_with_both_owners_named(tmp_path):
    broker, led, now, llm_peer, big2_peer = _broker(tmp_path)
    _reload_sequence(broker, now)

    assert ("evict", "llm") in llm_peer.calls     # the room was made, for r2
    assert ("warm", "llm") in llm_peer.calls      # and llm came back, for r3

    records = led.read()
    evict_llm = next(r for r in records
                     if r["decision"] == "evict" and r["kind"] == "llm")
    assert evict_llm["request"]["caused_by"] == "benchday:acct_b", \
        "benchday's request needed the room"

    load_llm = next(r for r in records
                    if r["decision"] == "load" and r["kind"] == "llm"
                    and (r.get("ts") or 0) > evict_llm["ts"])
    assert load_llm["request"]["caused_by"] == "attune:acct_a", \
        "attune's request brought it back"


def test_the_request_id_field_is_named_request_id_not_owner(tmp_path):
    broker, led, now, *_ = _broker(tmp_path)
    broker.plan_and_apply([Request(id="r1", kind="llm",
                                   owner="attune:acct_a",
                                   owner_asserted=True)], {})
    grant = next(r for r in led.read() if r["decision"] == "grant")
    req = grant["request"]
    assert req["request_id"] == "r1"
    assert req["owner"] == "attune:acct_a"
    assert req["request_id"] != req["owner"], \
        "the old key stored the id under `owner`; the two must not collide"
    assert req["owner_asserted"] is True


def test_explain_reload_attributes_the_reload_from_the_ledger_alone(tmp_path):
    """C.3 in one breath: the gate script reads the same ledger and prints the
    owner pair with no journal consulted."""
    import importlib.util
    import io
    import os
    broker, led, now, *_ = _broker(tmp_path)
    _reload_sequence(broker, now)

    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "explain_reload.py")
    spec = importlib.util.spec_from_file_location(
        "explain_reload_under_test", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = io.StringIO()
    assert mod.explain(led.path, "llm", out=out) == 0
    text = out.getvalue()
    assert "evict" in text and "caused_by=benchday:acct_b" in text
    assert "load" in text and "caused_by=attune:acct_a" in text

    # And the empty cases refuse silently-ok answers:
    out2 = io.StringIO()
    assert mod.explain(led.path, "never-heard-of-it", out=out2) == 1
    assert "never evicted" in out2.getvalue()
