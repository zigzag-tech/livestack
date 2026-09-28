"""`plan()` records can be re-run, not only read (spec unit-composition,
"A `plan()` decision can be replayed from its record").

scheduler-policy-routine design §10: "today's host ledger records lack unit
footprints and device capacities, so `plan()` cannot be replayed from them".
These drive the real HostBroker, so the record under test is the one
production writes."""
import os

from livestack_node.hostbroker import HostBroker
from livestack_node.ledger import JsonlLedger, validate
from livestack_node.planner import Device, Placement, Residency, Unit, WorldState, plan
from livestack_node.replay_plan import replay
from livestack_node.snapshots import SnapshotStore, snapshot_hash, world_from_json, world_to_json


class Peer:
    def __init__(self, unit, resident=False, device="h/gpu0"):
        self.host_id, self.device_id, self._unit, self._resident = "h", device, unit, resident
        self.calls = []

    def units(self):
        return {self._unit.kind: self._unit}

    def placements(self):
        return [Placement(self._unit.kind, self.device_id, loaded_at=0)] if self._resident else []

    def warm(self, kind, device=None, budget=None):
        self.calls.append(("warm", kind))
        self._resident = True

    def evict(self, kind):
        self.calls.append(("evict", kind))
        self._resident = False


def _world():
    return WorldState(
        devices=(Device("h/gpu0", "h", capacity={"vram_bytes": 24}),),
        units={"llm": Unit("llm", {"vram_bytes": 21}, priority=10, residency=Residency.SOFT_PIN,
                           attributes={"class": "llm"}, servable_on=frozenset({"h/gpu0"}),
                           footprint_source="vllm-startup"),
               "embed": Unit("embed", {"vram_bytes": 4}, priority=50)},
        placements=(Placement("embed", "h/gpu0"),), now=100.0,
        demand={"llm": 2.0}, measured_free={"h/gpu0": {"vram_bytes": 20}})


def test_world_round_trips_exactly():
    w = _world()
    payload = world_to_json(w)
    back, _ = world_from_json(payload)
    assert back == w
    assert snapshot_hash(world_to_json(back)) == snapshot_hash(payload)


def test_identical_states_share_one_file_and_the_cap_evicts_oldest(tmp_path):
    s = SnapshotStore(str(tmp_path), max_bytes=10**9)
    h1, h2 = s.put(_world()), s.put(_world())
    assert h1 == h2 and len(os.listdir(tmp_path)) == 1
    small = SnapshotStore(str(tmp_path / "small"), max_bytes=1)
    small.put(_world())
    other = WorldState(devices=_world().devices, units=_world().units, now=200.0)
    small.put(other)
    assert len(os.listdir(tmp_path / "small")) <= 1


def test_every_plan_row_points_at_a_snapshot_that_replays(tmp_path):
    led = JsonlLedger(str(tmp_path / "host.jsonl"))
    store = SnapshotStore(str(tmp_path / "snaps"))
    llm = Peer(Unit("llm", {"vram_bytes": 21}, priority=10, residency=Residency.HARD_PIN,
                    min_resident=1))
    embed = Peer(Unit("embed", {"vram_bytes": 8}, priority=50), resident=True)
    br = HostBroker([Device("h/gpu0", "h", capacity={"vram_bytes": 24})], [llm, embed],
                    clock=lambda: 1000.0, ledger=led, snapshot_store=store)
    br.plan_and_apply([])
    rows = [r for r in led.read() if r["decision"] in ("load", "evict", "grant", "defer")]
    assert rows, "the HARD_PIN floor should have produced a plan"
    assert all(r.get("snapshot", "").startswith("sha256:") for r in rows)
    assert all(validate(r) == [] for r in rows)
    # The candidate rows now say what each unit weighed and where that came from.
    assert any("footprint" in c["reason"] and "(declared)" in c["reason"]
               for r in rows for c in r["candidates"])
    out = replay(rows, store)
    assert out["plans"] == 1 and out["matched"] == 1 and out["mismatched"] == 0


def test_a_row_without_a_snapshot_is_counted_not_matched(tmp_path):
    out = replay([{"decision": "load", "kind": "x", "ts": 1.0}], SnapshotStore(str(tmp_path)))
    assert out["rows_without_snapshot"] == 1 and out["matched"] == 0
