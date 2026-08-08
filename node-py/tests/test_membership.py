"""Peer membership: who is on this host, and who has gone.

The defect these pin down (see `_plans/peer-membership.md`): the broker took a
static peer list, so a node that existed was invisible and a node that had gone
was polled every cycle forever, logging one identical line each time — 92,089 of
them on xc-mac-studio — with no record of how long it had been absent.
"""
from livestack_node.hostbroker import HostBroker, peer_key
from livestack_node.membership import (
    MembershipPolicy, PeerRoster, RosterFull, classify, probe_interval,
    FRESH, SUSPECT, MIA, SOURCE_SEED, SOURCE_REGISTERED,
)
from livestack_node.planner import Device, Placement, Unit, Residency


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakePeer:
    """A peer that can be told to start failing, so absence is a behaviour
    rather than a mock assertion."""

    def __init__(self, base, host="h", device="gpu0", kind="asr"):
        self.base = base
        self.host_id = host
        self.device_id = device
        self._unit = Unit(kind, {"vram": 4}, priority=10, residency=Residency.HARD_PIN)
        self.up = True
        self.probes = 0

    def units(self):
        self.probes += 1
        if not self.up:
            raise ConnectionError("Connection refused")
        return {self._unit.kind: self._unit}

    def placements(self):
        return [Placement(self._unit.kind, self.device_id, busy=False)]

    def device_memory(self):
        return None

    def device_capacity(self):
        return None


POLICY = MembershipPolicy(suspect_after_s=45, mia_after_s=600,
                          suspect_probe_every_s=30, mia_probe_every_s=120)


# -- the pure state machine ---------------------------------------------------

def test_state_is_derived_from_elapsed_time_not_cycle_counts():
    assert classify(0, POLICY) == FRESH
    assert classify(44, POLICY) == FRESH
    assert classify(45, POLICY) == SUSPECT
    assert classify(599, POLICY) == SUSPECT
    assert classify(600, POLICY) == MIA


def test_any_single_success_returns_a_peer_to_fresh_immediately():
    """Membership is a report, not a promise — so it re-admits on one proof.
    (Route emission is the opposite and must not copy this.)"""
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    clock.advance(700)
    assert roster.state_of("p") == MIA
    roster.mark_seen("p")
    assert roster.state_of("p") == FRESH


def test_a_failed_probe_is_not_evidence_of_life():
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    clock.advance(100)
    roster.mark_probed("p")          # attempted, failed
    assert roster.state_of("p") == SUSPECT   # still aging from last SUCCESS
    clock.advance(600)
    assert roster.state_of("p") == MIA


def test_transitions_are_logged_once_not_once_per_cycle():
    """The 92,089-line failure, pinned. 'Still gone' is not an event."""
    clock = Clock()
    lines = []
    roster = PeerRoster(POLICY, clock=clock, log=lines.append)
    roster.seed("p")
    lines.clear()
    clock.advance(50)
    for _ in range(200):             # 200 reconcile cycles with the peer down
        roster.mark_probed("p")
    assert len(lines) == 1
    assert "suspect" in lines[0]
    clock.advance(600)
    for _ in range(200):
        roster.mark_probed("p")
    assert len(lines) == 2
    assert "mia" in lines[1]


def test_backoff_slows_probing_of_an_absent_peer():
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    assert roster.due_for_probe("p")      # fresh: every cycle
    roster.mark_probed("p")
    clock.advance(50)                      # now suspect
    roster.mark_probed("p")
    clock.advance(10)
    assert not roster.due_for_probe("p")   # 10s < 30s suspect cadence
    clock.advance(25)
    assert roster.due_for_probe("p")


# -- seeds vs registrations ---------------------------------------------------

def test_a_registered_peer_is_pruned_but_a_seed_never_is():
    """An operator's seed is a statement that the node ought to exist; deleting
    it would silently undo that. A registered peer re-announces on return."""
    clock = Clock()
    policy = MembershipPolicy(suspect_after_s=45, mia_after_s=600, prune_after_s=3600)
    roster = PeerRoster(policy, clock=clock)
    roster.seed("seeded")
    roster.register("announced")
    clock.advance(7200)
    assert roster.prunable() == ["announced"]


def test_an_unset_prune_window_deletes_nothing():
    """A delete-shaped bound must never default to deleting."""
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(prune_after_s=None), clock=clock)
    roster.register("announced")
    clock.advance(10_000_000)
    assert roster.prunable() == []


def test_a_seed_that_self_registers_stays_a_seed():
    roster = PeerRoster(POLICY, clock=Clock())
    roster.seed("p")
    roster.register("p")
    assert roster.snapshot()[0]["source"] == SOURCE_SEED


def test_registration_is_idempotent_on_the_facade_url():
    roster = PeerRoster(POLICY, clock=Clock())
    roster.register("http://127.0.0.1:8100/livestack")
    roster.register("http://127.0.0.1:8100/livestack")
    assert len(roster.snapshot()) == 1


def test_roster_refuses_to_grow_past_its_cap():
    roster = PeerRoster(MembershipPolicy(max_peers=2), clock=Clock())
    roster.register("a")
    roster.register("b")
    try:
        roster.register("c")
        assert False, "expected RosterFull"
    except RosterFull as e:
        assert "full" in str(e)
    assert len(roster.snapshot()) == 2


# -- the broker end to end ----------------------------------------------------

def test_a_node_that_announces_itself_becomes_plannable():
    """The north star: starting a model server is the only action required."""
    clock = Clock()
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=clock)
    assert broker.snapshot([]).units == {}

    peer = FakePeer("http://127.0.0.1:8100/livestack")
    broker.register_url(peer.base, make_peer=lambda _: peer,
                        host_id="h", device_id="gpu0", kinds=["asr"])

    world = broker.snapshot([])
    assert "asr" in world.units
    assert broker.roster.state_of(peer.base) == FRESH


def test_a_peer_that_goes_away_is_reported_with_a_duration():
    clock = Clock()
    peer = FakePeer("http://127.0.0.1:8100/livestack")
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[peer], clock=clock,
                        membership=POLICY)
    broker.snapshot([])
    peer.up = False
    clock.advance(700)
    broker.snapshot([])

    row = broker.membership_snapshot()[0]
    assert row["state"] == MIA
    assert row["unseen_seconds"] >= 700
    assert "Connection refused" in row["last_error"]


def test_a_down_peer_does_not_blind_the_arbiter():
    """Pre-existing guarantee that must not regress: plan over the survivors."""
    clock = Clock()
    up = FakePeer("http://a/livestack", kind="asr")
    down = FakePeer("http://b/livestack", kind="tts")
    down.up = False
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[up, down], clock=clock, membership=POLICY)
    world = broker.snapshot([])
    assert "asr" in world.units
    assert "tts" not in world.units


def test_backoff_stops_hammering_a_dead_peer_every_cycle():
    """9.2 MB of log and a connect attempt every 5s for weeks was the old cost
    of a peer that was simply not there."""
    clock = Clock()
    dead = FakePeer("http://gone/livestack")
    dead.up = False
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[dead], clock=clock, membership=POLICY)
    for _ in range(120):                 # 120 reconcile ticks, 5s apart
        broker.snapshot([])
        clock.advance(5)
    # 600s of downtime: a handful of probes on the backoff cadence, not 120.
    assert dead.probes < 30, f"probed {dead.probes} times — backoff is not working"


def test_pruning_removes_the_peer_object_too():
    clock = Clock()
    peer = FakePeer("http://gone/livestack")
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=clock,
                        membership=MembershipPolicy(prune_after_s=3600))
    broker.register_url(peer.base, make_peer=lambda _: peer)
    peer.up = False
    clock.advance(7200)
    assert broker.prune_absent() == [peer.base]
    assert broker.peers == []
    assert broker.membership_snapshot() == []
