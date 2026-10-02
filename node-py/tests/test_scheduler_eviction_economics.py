from livestack_node.planner import Device, Unit, Placement, Request, WorldState, Evict, Grant, plan

def test_recent_demand_prices_eviction_across_different_workload_groups():
    w = WorldState(devices=(Device("a-hot", "h", {"vram": 24}), Device("b-cold", "h", {"vram": 24})),
        units={"hot": Unit("hot", {"vram": 22}, priority=30, reload_cost=120, spread_group="llm"),
               "cold": Unit("cold", {"vram": 22}, priority=30, reload_cost=120, spread_group="other"),
               "new": Unit("new", {"vram": 21}, priority=30, spread_group="image")},
        placements=(Placement("hot", "a-hot"), Placement("cold", "b-cold")),
        requests=(Request("r", "new", created_at=100),), now=100, demand={"hot": 100, "cold": 1})
    assert plan(w).of(Grant)[0].device_id == "b-cold"


def test_request_on_other_device_does_not_empty_pressure_device():
    w = WorldState(devices=(Device("hot-gpu", "tower", {"vram": 24}, reserved={"vram": 2}), Device("image-gpu", "joe", {"vram": 8})),
        units={"hot": Unit("hot", {"vram": 23}, priority=30, servable_on=frozenset(("hot-gpu",))),
               "image": Unit("image", {"vram": 4}, priority=30, servable_on=frozenset(("image-gpu",)))},
        placements=(Placement("hot", "hot-gpu"),),
        requests=(Request("r", "image", created_at=100),), now=100)
    assert not plan(w).of(Evict)
    assert plan(w).of(Grant)[0].device_id == "image-gpu"


def test_requirement_admission_records_demand_for_actual_granted_kind():
    from test_hostbroker import FakePeer
    from livestack_node.hostbroker import HostBroker
    peer = FakePeer("h", "gpu", Unit("hot", {"vram": 2}, attributes={"model": "wanted"}), resident=True)
    broker = HostBroker([Device("gpu", "h", {"vram": 8})], [peer], clock=lambda: 100)
    assert broker.admit(Request("r", "", requires={"model": "wanted"})) == "gpu"
    assert broker.demand().get("hot") == 1
    assert "" not in broker.demand()
