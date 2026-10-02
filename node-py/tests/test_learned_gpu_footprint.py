"""A unit's GPU footprint is learned from its node's allocator, not declared.

Real HTTP (uvicorn on an ephemeral port), real store files, the real broker
peer (`RestPeer`) and the real planner. The only stand-in is the allocator
meter, scripted the way torch's counters move: a load grows `reserved`, an op
pushes the peak above it, and fragmentation makes reserved grow more than
allocated. (No GPU in CI; the deploy record has the same numbers off a 2070.)
"""
import json
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")

from fastapi import FastAPI  # noqa: E402
from livestack_node import ManagedUnit, ResidencyPolicy, attach, measure, noop_free  # noqa: E402
from livestack_node.hostbroker import RestPeer  # noqa: E402
from livestack_node.planner import (Device, Placement, Unit, WorldState,  # noqa: E402
                                    _World)

GB = 1_000_000_000
DECLARED = 3 * GB
UNIT = "klein"


class Allocator:
    """torch.cuda's counters, scripted: allocated/reserved and their peaks."""
    def __init__(self):
        self.alloc = self.res = 0
        self.max_alloc = self.max_res = 0

    def grow(self, alloc, res):
        self.alloc += alloc
        self.res += res
        self.max_alloc = max(self.max_alloc, self.alloc)
        self.max_res = max(self.max_res, self.res)

    def spike(self, alloc, res):          # an op: peaks rise, then it frees
        self.max_alloc = max(self.max_alloc, self.alloc + alloc)
        self.max_res = max(self.max_res, self.res + res)

    def reset_peak(self):
        self.max_alloc, self.max_res = self.alloc, self.res

    def allocated(self):
        return self.alloc

    def max_allocated(self):
        return self.max_alloc

    def reserved(self):
        return self.res

    def max_reserved(self):
        return self.max_res


def node(monkeypatch, store, meter, load_bytes=(int(4.3 * GB), int(4.5 * GB))):
    monkeypatch.setattr(measure, "alloc_meter", lambda: meter)
    monkeypatch.setenv("LIVESTACK_ACT_STORE", str(store))
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")

    def load():
        meter.grow(*load_bytes)
        return "weights"

    def free():
        meter.grow(-load_bytes[0], -load_bytes[1])

    unit = ManagedUnit(UNIT, load, free, footprint=DECLARED,
                       residency_policy=ResidencyPolicy.UNPINNED)
    app = FastAPI()
    manager, _ = attach(app, host_id="h", kind="imagegen", units={UNIT: unit},
                        idle_seconds=600, coload=True, gpu_call=lambda fn: fn(),
                        device_meter=None)
    return app, manager


class Served:
    def __init__(self, app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True).start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.server.started

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True

    def get(self, path):
        return httpx.get(self.base + path, timeout=5).json()

    def post(self, path, body):
        r = httpx.post(self.base + path, json=body, timeout=5)
        assert r.status_code == 200, r.text
        return r.json()

    def unit(self):
        (u,) = self.get("/livestack/residence")["units"]
        return u


def generate(manager, meter, alloc, res):
    manager.run(UNIT, lambda model: meter.spike(alloc, res))


def test_declared_prior_until_measured_then_resident_then_whole_cost(monkeypatch, tmp_path):
    meter = Allocator()
    app, manager = node(monkeypatch, tmp_path / "act.json", meter)
    with Served(app) as http:
        u = http.unit()
        assert u["footprint"] == {"vram_bytes": DECLARED}
        assert u["footprint_source"] == "declared"
        assert u["learned"] == {"declared_bytes": DECLARED, "state": "unmeasured"}

        http.post("/livestack/model/warm", {"unit": UNIT})
        u = http.unit()
        # Reserved growth across the load, not the declared number.
        assert u["footprint"] == {"vram_bytes": int(4.5 * GB)}
        assert u["footprint_source"] == "allocator-resident"
        assert u["learned"]["state"] == "resident-only"

        # The op's peak over its baseline: reserved (2.7 GB) beats allocated (1.9 GB).
        generate(manager, meter, int(1.9 * GB), int(2.7 * GB))
        u = http.unit()
        assert u["footprint_source"] == "allocator"
        assert u["activation_headroom"] == {"vram_bytes": int(2.7 * GB)}
        assert u["learned"] == {"declared_bytes": DECLARED, "state": "measured",
                                "resident_bytes": int(4.5 * GB),
                                "activation_bytes": int(2.7 * GB)}

    stored = json.loads((tmp_path / "act.json").read_text())
    assert stored["resident"] == {UNIT: 4.5 * GB}
    assert stored["units"] == {UNIT: 2.7 * GB}


def test_learned_values_survive_restart_and_only_rise(monkeypatch, tmp_path):
    store = tmp_path / "act.json"
    meter = Allocator()
    app, manager = node(monkeypatch, store, meter)
    manager.ensure(UNIT)
    generate(manager, meter, GB, int(2.7 * GB))

    # Restart: a fresh process, nothing loaded yet. The learned cost stands.
    meter2 = Allocator()
    app2, manager2 = node(monkeypatch, store, meter2, load_bytes=(int(3 * GB), int(3.2 * GB)))
    with Served(app2) as http:
        u = http.unit()
        assert u["resident"] is False
        assert u["footprint"] == {"vram_bytes": int(4.5 * GB)}
        assert u["footprint_source"] == "allocator"
        # A smaller load and a smaller op are not evidence that the model shrank.
        http.post("/livestack/model/warm", {"unit": UNIT})
        generate(manager2, meter2, GB, GB)
        u = http.unit()
        assert u["footprint"] == {"vram_bytes": int(4.5 * GB)}
        assert u["activation_headroom"] == {"vram_bytes": int(2.7 * GB)}


def test_a_larger_measurement_raises_it(monkeypatch, tmp_path):
    store = tmp_path / "act.json"
    meter = Allocator()
    _, manager = node(monkeypatch, store, meter)
    manager.ensure(UNIT)
    manager.unload_now()
    meter2 = Allocator()
    _, manager2 = node(monkeypatch, store, meter2, load_bytes=(5 * GB, int(5.2 * GB)))
    manager2.ensure(UNIT)
    assert json.loads(store.read_text())["resident"] == {UNIT: 5.2 * GB}


def test_a_load_this_allocator_cannot_see_is_not_a_zero_footprint(monkeypatch, tmp_path):
    # An engine in another process (a vLLM proxy) moves nothing here.
    meter = Allocator()
    app, manager = node(monkeypatch, tmp_path / "act.json", meter, load_bytes=(0, 0))
    with Served(app) as http:
        http.post("/livestack/model/warm", {"unit": UNIT})
        u = http.unit()
        assert u["footprint"] == {"vram_bytes": DECLARED}
        assert u["footprint_source"] == "declared"
        assert u["learned"]["state"] == "unmeasured"


def test_an_unreadable_store_is_a_failure_not_an_absence(monkeypatch, tmp_path):
    store = tmp_path / "act.json"
    store.write_text("{not json")
    meter = Allocator()
    app, manager = node(monkeypatch, store, meter)
    with Served(app) as http:
        u = http.unit()
        # Safe default: the declared prior. And it says WHY there is no measurement.
        assert u["footprint"] == {"vram_bytes": DECLARED}
        assert u["footprint_source"] == "declared"
        assert u["learned"]["state"] == "failed"
        assert "unreadable" in u["learned"]["error"]
    assert (tmp_path / "act.json.corrupt").read_text() == "{not json"


@pytest.mark.parametrize("bad", ['{"signature": %s, "resident": {"klein": -5}}',
                                 '{"signature": %s, "resident": {"klein": 1e300}}',
                                 '{"signature": %s, "units": ["klein"]}'])
def test_an_invalid_store_value_is_refused(monkeypatch, tmp_path, bad):
    from livestack_node.serve import _footprint_signature
    sig = json.dumps(_footprint_signature({UNIT: ManagedUnit(UNIT, lambda: 0, noop_free,
                                                              footprint=DECLARED)}))
    store = tmp_path / "act.json"
    store.write_text(bad % sig)
    app, _ = node(monkeypatch, store, Allocator())
    with Served(app) as http:
        u = http.unit()
        assert u["footprint"] == {"vram_bytes": DECLARED}
        assert u["learned"]["state"] == "failed"
        assert "invalid" in u["learned"]["error"]


def test_the_store_is_bounded_to_this_process_units(tmp_path):
    t = measure.ActivationTracker(store_path=str(tmp_path / "s.json"), signature="s",
                                  known_units=[UNIT])
    t.record_resident(UNIT, 4 * GB)
    t.record_resident("someone-else", 9 * GB)
    t.record("someone-else", 9 * GB)
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["resident"] == {UNIT: 4 * GB} and data["units"] == {}


def test_broker_plans_with_the_measured_footprint(monkeypatch, tmp_path):
    meter = Allocator()
    app, manager = node(monkeypatch, tmp_path / "act.json", meter)
    with Served(app) as http:
        http.post("/livestack/model/warm", {"unit": UNIT})
        peer = RestPeer(http.base + "/livestack")
        assert peer.units()[UNIT].footprint_source == "allocator-resident"
        generate(manager, meter, GB, int(2.7 * GB))
        unit = peer.units()[UNIT]
    assert unit.footprint == {"vram_bytes": 4.5 * GB}
    assert unit.activation_headroom == {"vram_bytes": 2.7 * GB}
    assert unit.footprint_source == "allocator"

    card = Device("h/gpu0", "h", capacity={"vram_bytes": 8 * GB},
                  reserved={"vram_bytes": 1 * GB})
    world = _World(WorldState(devices=(card,), units={UNIT: unit},
                              placements=(Placement(UNIT, card.id),)))
    # Fully measured: the reserve no longer double-covers activation the
    # headroom already reserves. 8 - 4.5 resident - 2.7 activation.
    assert world.reserve(card.id) == {}
    assert world.free(card.id)["vram_bytes"] == pytest.approx(0.8 * GB)
    # Resident measured, activation not yet: the reserve still covers it.
    from dataclasses import replace
    half = replace(unit, footprint_source="allocator-resident", activation_headroom={})
    world = _World(WorldState(devices=(card,), units={UNIT: half},
                              placements=(Placement(UNIT, card.id),)))
    assert world.reserve(card.id) == {"vram_bytes": 1 * GB}
