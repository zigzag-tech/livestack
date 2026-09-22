"""Unit economics thread end to end: unit file -> ManagedUnit -> /residence
-> RestPeer.units() -> planner Unit.

The 27B's measured numbers are the motivating case (HARMONY.md, "Unit
economics, declared"): a ~50 s reload protected for 15 s and tie-broken at
1.0 was evicted by a 0.6 B embedder 20 s after it loaded. Declared
`min_residency_s: 60, reload_cost: 4` fix that. ABSENT values must keep
today's defaults exactly — a node that declares nothing is byte-for-byte the
node it was.
"""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node import (  # noqa: E402
    ManagedUnit, ResidencyPolicy, attach, noop_free,
)


def _app(**unit_kw):
    units = {
        "llm27": ManagedUnit("llm27", loader=lambda: "m", freer=noop_free,
                             footprint=21 * (1 << 30),
                             residency_policy=ResidencyPolicy.UNPINNED,
                             **unit_kw),
        "embed": ManagedUnit("embed", loader=lambda: "m", freer=noop_free,
                             footprint=3 * (1 << 30),
                             residency_policy=ResidencyPolicy.UNPINNED),
    }
    app = FastAPI()
    attach(app, host_id="h", kind="llm", units=units, idle_seconds=120,
           coload=True, gpu_call=lambda fn: fn(), device_meter=None)
    return app


def test_declared_economics_reach_the_residence_report():
    app = _app(min_residency_s=60, reload_cost=4, priority=20)
    res = TestClient(app).get("/livestack/residence").json()
    by_kind = {u["kind"]: u for u in res["units"]}
    assert by_kind["llm27"]["min_residency_s"] == 60.0
    assert by_kind["llm27"]["reload_cost"] == 4.0
    assert by_kind["llm27"]["priority"] == 20
    # The undeclared sibling omits the fields entirely.
    assert "min_residency_s" not in by_kind["embed"]
    assert "reload_cost" not in by_kind["embed"]
    assert "priority" not in by_kind["embed"]


def test_the_broker_turns_the_report_into_a_planner_unit():
    from livestack_node.hostbroker import RestPeer

    app = _app(min_residency_s=60, reload_cost=4, priority=20)
    residence = TestClient(app).get("/livestack/residence").json()

    peer = RestPeer("http://h:8100/livestack")
    peer.refresh = lambda: residence          # serve the snapshot /residence served
    units = peer.units()
    assert units["llm27"].min_residency_s == 60.0
    assert units["llm27"].reload_cost == 4.0
    assert units["llm27"].priority == 20
    # Declared priority outranks the tier-derived default: UNPINNED would be
    # 30 under _RES_TO_PRIO; the node's measured claim of 20 wins.
    assert units["embed"].priority == 30        # UNPINNED default, unchanged
    assert units["embed"].min_residency_s == 15.0
    assert units["embed"].reload_cost == 1.0


def test_reported_footprint_wins_and_legacy_default_only_fills_zero():
    from livestack_node.hostbroker import RestPeer

    peer = RestPeer("http://node", fallback_footprints={"qwen": 9_000, "old": 7_000})
    peer.refresh = lambda: {"units": [
        {"kind": "qwen", "footprint": {"vram_bytes": 3_000}, "residency": 2},
        {"kind": "old", "footprint": {"vram_bytes": 0}, "residency": 2},
    ]}
    units = peer.units()
    assert units["qwen"].footprint == {"vram_bytes": 3_000}
    assert units["old"].footprint == {"vram_bytes": 7_000}


def test_the_unit_file_accepts_the_three_fields(monkeypatch):
    """The harmony-llm unit file parser: declared fields land on the spec and
    the ManagedUnit; absent fields stay None and never reach /residence."""
    import importlib.util
    import json
    import os

    units = [{
        "name": "bench_qwen38_27b", "model": "m", "port": 8199,
        "footprint_gb": 21, "min_residency_s": 60, "reload_cost": 4,
        "priority": 20,
    }, {
        "name": "embed_multi", "model": "m", "port": 8205,
        "footprint_gb": 3,
    }]
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "examples", "harmony-llm",
        "server.py"))
    monkeypatch.setenv("HARMONY_LLM_UNITS", json.dumps(units))
    monkeypatch.setenv("HARMONY_LLM_WARM_ON_START", "0")
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_ACT_SAMPLE_S", "0")
    # The file variable wins over the inline one; test_harmony_llm_routing
    # leaks it into os.environ globally, so clear it or the import below
    # would read that file instead of our inline units.
    monkeypatch.delenv("HARMONY_LLM_UNITS_FILE", raising=False)
    spec = importlib.util.spec_from_file_location(
        "harmony_llm_econ_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    big = mod.SPECS["bench_qwen38_27b"]
    assert big["min_residency_s"] == 60
    assert big["reload_cost"] == 4
    assert big["priority"] == 20
    # Declared-or-not, every spec carries the keys; undeclared is None.
    assert mod.SPECS["embed_multi"]["min_residency_s"] is None
    assert mod.SPECS["embed_multi"]["reload_cost"] is None
    assert mod.SPECS["embed_multi"]["priority"] is None

    managed = mod._UNITS["bench_qwen38_27b"]
    assert managed.min_residency_s == 60
    assert managed.reload_cost == 4
    assert managed.priority == 20
    assert mod._UNITS["embed_multi"].min_residency_s is None

    res = TestClient(mod.app).get("/livestack/residence").json()
    by_kind = {u["kind"]: u for u in res["units"]}
    assert by_kind["bench_qwen38_27b"]["min_residency_s"] == 60.0
    assert "min_residency_s" not in by_kind["embed_multi"]


def test_a_young_declared_load_defers_naming_the_residency_floor():
    """The mechanism F.3 reads on the live box: a 27B 20 s into a declared
    60 s floor is asked to move by an embedder -> the request DEFERS, and the
    record names the floor (a wait, not a refusal of the request's worth).
    Once the floor passes, ordinary rules apply again."""
    from livestack_node.planner import (
        Defer, Device, Placement, Request, Residency, Unit, WorldState, plan,
    )
    GB = 1_000_000_000
    llm27 = Unit("llm27", {"vram_bytes": 20 * GB}, priority=20,
                 min_residency_s=60.0)
    embed = Unit("embed", {"vram_bytes": 6 * GB}, priority=30)

    def world(now, loaded_at):
        return WorldState(
            devices=(Device(id="h/gpu0", host_id="h",
                            capacity={"vram_bytes": 24 * GB},
                            reserved={"vram_bytes": 2 * GB}),),
            units={"llm27": llm27, "embed": embed},
            placements=(Placement(kind="llm27", device_id="h/gpu0",
                                  loaded_at=loaded_at),),
            requests=(Request(id="r1", kind="embed", owner="attune:acct_a"),),
            now=now)

    # 20 s into the 60 s floor: the only thing between embed and the card is
    # the floor, and the Defer says so.
    p = plan(world(now=1020.0, loaded_at=1000.0))
    defer = next(a for a in p.actions if isinstance(a, Defer))
    assert "residency floor" in defer.reason
    assert "llm27" in defer.reason and "60" in defer.reason
    assert not [a for a in p.actions
                if a.__class__.__name__ == "Evict"], "protected"

    # 70 s in: past the floor, the embedder may preempt (lower number wins).
    p2 = plan(world(now=1070.0, loaded_at=1000.0))
    assert not [a for a in p2.actions if isinstance(a, Defer)]
    assert any(a.kind == "llm27" for a in p2.actions
               if a.__class__.__name__ == "Evict")
