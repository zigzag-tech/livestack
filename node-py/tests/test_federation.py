"""Cross-host federation: one HostBroker over peers on several hosts. Devices are
discovered from the peers (one per reported device_id); the same plan() routes a
request to the host with room (migrate) or preempts the cheaper victim across the
fleet, then dispatches to the owning host's node."""
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Unit, Placement, Request, Residency

GB = 1_000_000_000


class FedPeer:
    def __init__(self, host_id, device_id, units, resident=None):
        self.host_id = host_id
        self.device_id = device_id
        self._units = units
        self._resident = dict(resident or {})   # kind -> busy
        self.calls = []

    def units(self): return self._units
    def placements(self):
        return [Placement(k, self.device_id, loaded_at=0, busy=b) for k, b in self._resident.items()]
    def warm(self, k): self.calls.append(("warm", k))
    def evict(self, k): self.calls.append(("evict", k))


def test_routes_to_host_with_room_instead_of_preempting():
    worker = Unit("worker", {"vram_bytes": 10 * GB}, priority=20)
    filler = Unit("filler", {"vram_bytes": 10 * GB}, priority=30, residency=Residency.UNPINNED)
    a = FedPeer("host-a", "host-a/gpu0", {"worker": worker, "filler": filler}, resident={"filler": False})
    b = FedPeer("host-b", "host-b/gpu0", {"worker": worker})
    cfg = {"host-a/gpu0": {"vram_bytes": 11 * GB, "reserved": 0},
           "host-b/gpu0": {"vram_bytes": 24 * GB, "reserved": 0}}
    broker = HostBroker(devices=None, peers=[a, b], device_config=cfg, clock=lambda: 1000.0)
    dev = broker.admit(Request("r1", "worker", created_at=1000))
    assert dev == "host-b/gpu0"                         # placed where there's room
    assert ("warm", "worker") in b.calls               # dispatched to host-b's node
    assert all(c[0] != "evict" for c in a.calls)       # host-a NOT preempted (migrate is cheaper)


def test_preempts_cheaper_victim_across_the_fleet():
    # Both hosts full of idle low-priority filler; the high-priority job preempts the
    # cheaper-to-reload victim (fb on host-b) and is dispatched there.
    worker = Unit("worker", {"vram_bytes": 10 * GB}, priority=10)
    fa = Unit("fa", {"vram_bytes": 10 * GB}, priority=30, residency=Residency.UNPINNED, reload_cost=9)
    fb = Unit("fb", {"vram_bytes": 10 * GB}, priority=30, residency=Residency.UNPINNED, reload_cost=1)
    a = FedPeer("host-a", "host-a/gpu0", {"worker": worker, "fa": fa}, resident={"fa": False})
    b = FedPeer("host-b", "host-b/gpu0", {"worker": worker, "fb": fb}, resident={"fb": False})
    cfg = {"host-a/gpu0": {"vram_bytes": 11 * GB, "reserved": 0},
           "host-b/gpu0": {"vram_bytes": 11 * GB, "reserved": 0}}
    broker = HostBroker(devices=None, peers=[a, b], device_config=cfg, clock=lambda: 1000.0)
    dev = broker.admit(Request("r1", "worker", created_at=1000))
    assert dev == "host-b/gpu0"
    assert ("evict", "fb") in b.calls
    assert ("warm", "worker") in b.calls
    assert all(c[0] != "evict" for c in a.calls)        # host-a's fa (costlier reload) spared


def test_devices_discovered_from_peers():
    u = Unit("m", {"vram_bytes": 1 * GB}, priority=20)
    peers = [FedPeer("h1", "h1/gpu0", {"m": u}), FedPeer("h2", "h2/gpu0", {"m": u})]
    broker = HostBroker(devices=None, peers=peers, clock=lambda: 0.0)
    w = broker.snapshot()
    assert sorted(d.id for d in w.devices) == ["h1/gpu0", "h2/gpu0"]
