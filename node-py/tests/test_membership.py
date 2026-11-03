"""Peer membership: who is on this host, and who has gone.

The defect these pin down (see `_plans/peer-membership.md`): the broker took a
static peer list, so a node that existed was invisible and a node that had gone
was polled every cycle forever, logging one identical line each time — 92,089 of
them on xc-mac-studio — with no record of how long it had been absent.
"""
import pytest

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


# -- event-driven demotion (Phase 8: a NAMED degradation demotes on the event)

def test_a_named_degradation_demotes_on_the_event_not_the_age():
    """A dial that dies with a NAMED degradation (mesh_tunnel_down,
    relay_quota) demotes fresh→suspect at age 0. The exception is stronger
    evidence than silence: over a tunnel a refused door says the node was
    never asked, and the fast re-probe is what catches a relay restart in
    seconds instead of discovering it at the next age threshold. Age still
    owns mia — only sustained unreachability drops placements."""
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    roster.mark_degraded("p", probe_every_s=10.0)
    assert roster.state_of("p") == SUSPECT        # age 0, already suspect
    assert not roster.due_for_probe("p")          # fast cadence armed instead
    clock.advance(10)
    assert roster.due_for_probe("p")


def test_event_demotion_clears_on_one_success():
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    roster.mark_degraded("p", probe_every_s=10.0)
    assert roster.state_of("p") == SUSPECT
    roster.mark_seen("p")
    assert roster.state_of("p") == FRESH
    # The override goes with the demotion: a fresh peer probes every cycle
    # again, not on the fast cadence.
    assert roster.due_for_probe("p")


def test_event_demotion_does_not_delay_or_hold_off_mia():
    """The override elevates fresh→suspect and nothing more: mia still lands
    at `mia_after_s` of unreachability, and at mia the fast cadence is
    dropped — a long-dead seed is never pruned, so without that drop it would
    be re-dialled every few seconds forever."""
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.seed("p")
    roster.mark_degraded("p", probe_every_s=10.0)
    clock.advance(600)
    assert roster.state_of("p") == MIA
    roster.mark_probed("p")
    assert not roster.due_for_probe("p")   # mia cadence (120s), not the override
    clock.advance(120)
    assert roster.due_for_probe("p")


def test_event_demotion_logs_the_transition_once_not_per_failure():
    """The 92,089-line discipline applies to the new rung too: one log line
    for the episode, no matter how many failed probes mark it."""
    clock = Clock()
    lines = []
    roster = PeerRoster(POLICY, clock=clock, log=lines.append)
    roster.seed("p")
    lines.clear()
    for _ in range(50):
        roster.mark_probed("p")
        roster.mark_degraded("p", probe_every_s=10.0)
        clock.advance(1)
    assert len(lines) == 1
    assert "suspect" in lines[0]


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


def test_a_seed_is_pruned_only_where_the_policy_opts_in():
    """A seed unseen for the whole window is gone (the field case: loopback
    duplicates of mesh-registered nodes sat `mia` for a week), but only a policy
    that says so forgets one."""
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(prune_after_s=3600, prune_seeds=True), clock=clock)
    roster.seed("seeded")
    roster.register("announced")
    clock.advance(3599)
    assert roster.prunable() == []
    clock.advance(2)
    assert sorted(roster.prunable()) == ["announced", "seeded"]


def test_a_peer_seen_again_restarts_its_window():
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(prune_after_s=3600, prune_seeds=True), clock=clock)
    roster.seed("seeded")
    clock.advance(3000)
    roster.mark_seen("seeded")
    clock.advance(3000)
    assert roster.prunable() == [], "6000s since start but only 3000s since last seen"


def test_the_daemon_default_prunes_after_a_day_and_off_is_explicit():
    """The deployed default is a stated number, not an implied one; disabling it
    is a deliberate act and a malformed value is refused, never guessed."""
    from livestack_node.membership import DEFAULT_PRUNE_AFTER_S, policy_from_env
    default = policy_from_env({})
    assert default.prune_after_s == DEFAULT_PRUNE_AFTER_S == 24 * 3600
    assert default.prune_seeds is True
    assert policy_from_env({"LIVESTACK_PEER_PRUNE_SECONDS": "7200"}).prune_after_s == 7200
    for off in ("0", "off", "OFF", "none"):
        policy = policy_from_env({"LIVESTACK_PEER_PRUNE_SECONDS": off})
        assert policy.prune_after_s is None and policy.prune_seeds is False
    for bad in ("-5", "soon", "nan"):
        with pytest.raises(ValueError):
            policy_from_env({"LIVESTACK_PEER_PRUNE_SECONDS": bad})
    assert MembershipPolicy().prune_after_s is None, "the bare policy still deletes nothing"


def test_an_unset_prune_window_deletes_nothing():
    """A delete-shaped bound must never default to deleting."""
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(prune_after_s=None), clock=clock)
    roster.register("announced")
    clock.advance(10_000_000)
    assert roster.prunable() == []


def test_an_announce_registers_but_does_not_certify():
    """The 7-hour lie, pinned. xc-mac-studio's polyasr was dead from 03:00 to
    10:00 on 2026-09-05 and listed `fresh` the whole time, because `attach()`
    starts the registrar before the server binds and every renewal reset the
    age. A peer that announces and is never snapshotted must decay on the
    normal clock."""
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.register("p")
    # One grace window to be snapshotted — a brand-new record is not born stale.
    assert roster.state_of("p") == FRESH

    for _ in range(60):              # 30 minutes of renewals, never snapshotted
        clock.advance(30)
        roster.register("p")
    assert roster.state_of("p") == MIA, "a renewal must not advance the age"


def test_a_renewal_does_not_reset_the_age():
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.register("p")
    clock.advance(50)
    assert roster.state_of("p") == SUSPECT
    roster.register("p")             # the node keeps announcing
    assert roster.state_of("p") == SUSPECT


def test_a_successful_snapshot_still_restores_fresh_at_once():
    """The fix must not cost the re-admission property: certification is the
    snapshot, and one is enough."""
    clock = Clock()
    roster = PeerRoster(POLICY, clock=clock)
    roster.register("p")
    clock.advance(700)
    assert roster.state_of("p") == MIA
    roster.mark_seen("p")
    assert roster.state_of("p") == FRESH


def test_a_node_that_only_announces_never_becomes_fresh_at_the_broker():
    """End to end: the broker's roster must not report a peer whose facade
    never answers. This is the shape §3.4/4 of the fleet-broker plan verifies
    against a 10-line script that only POSTs /peers."""
    clock = Clock()
    dead = FakePeer("http://announcer/livestack")
    dead.up = False
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=clock, membership=POLICY)
    broker.register_url(dead.base, make_peer=lambda _: dead)
    clock.advance(50)
    broker.snapshot([])              # the probe fails
    broker.register_url(dead.base, make_peer=lambda _: dead)   # it keeps announcing
    assert broker.roster.state_of(dead.base) == SUSPECT


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


def test_a_full_roster_of_live_peers_still_refuses():
    roster = PeerRoster(MembershipPolicy(max_peers=2, mia_after_s=600), clock=Clock())
    roster.register("a")
    roster.register("b")
    with pytest.raises(RosterFull, match="none mia"):
        roster.register("c")


def test_a_mia_peer_yields_its_slot_to_a_live_announce():
    """xc-tower-ubuntu 2026-11-01: fleetd's 32 slots were full, nine held by a
    host down for hours, and the cap refused a live depth facade (:8217) while
    the 24 h prune window kept the dead rows."""
    clock = Clock()
    evicted = []
    roster = PeerRoster(MembershipPolicy(max_peers=3, mia_after_s=600),
                        clock=clock, on_evict=evicted.append)
    roster.register("old-dead")
    clock.advance(100)
    roster.register("dead")
    roster.register("live")
    clock.advance(700)              # old-dead unseen 800 s, dead 700 s: both mia
    roster.mark_seen("live")
    roster.register("newcomer")
    keys = [r["peer"] for r in roster.snapshot()]
    assert sorted(keys) == ["dead", "live", "newcomer"]
    assert evicted == ["old-dead"], "the longest-unseen mia peer goes first"


def test_a_suspect_peer_keeps_its_slot():
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(max_peers=1, suspect_after_s=45,
                                         mia_after_s=600), clock=clock)
    roster.register("blipping")
    clock.advance(300)              # suspect, not mia: it may well come back
    with pytest.raises(RosterFull):
        roster.register("newcomer")


def test_registered_rows_yield_before_seeds_and_seeds_only_when_allowed():
    clock = Clock()
    roster = PeerRoster(MembershipPolicy(max_peers=2, mia_after_s=600,
                                         prune_after_s=86400, prune_seeds=True),
                        clock=clock)
    roster.seed("seed")
    roster.register("announced")
    clock.advance(700)
    roster.register("n1")
    assert {r["peer"] for r in roster.snapshot()} == {"seed", "n1"}
    roster.mark_seen("n1")
    roster.register("n2")           # only the mia seed is left to yield
    assert {r["peer"] for r in roster.snapshot()} == {"n1", "n2"}

    keep = PeerRoster(MembershipPolicy(max_peers=1, mia_after_s=600), clock=clock)
    keep.seed("operator-said-so")
    clock.advance(700)
    with pytest.raises(RosterFull):
        keep.register("newcomer")   # prune_seeds off: a seed never yields


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


def test_a_live_announce_to_a_full_broker_displaces_a_dead_peer():
    clock = Clock()
    dead = FakePeer("http://100.64.0.3:8100/livestack")
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=clock,
                        membership=MembershipPolicy(max_peers=1, mia_after_s=600,
                                                    prune_after_s=86400))
    broker.register_url(dead.base, make_peer=lambda _: dead)
    dead.up = False
    clock.advance(700)
    fresh = FakePeer("http://100.64.0.18:8217/livestack", kind="image")
    out = broker.register_url(fresh.base, make_peer=lambda _: fresh)
    assert out["peer"] == fresh.base
    assert [p.base for p in broker.peers] == [fresh.base]
    assert [r["peer"] for r in broker.membership_snapshot()] == [fresh.base]
    assert "image" in broker.snapshot([]).units


def test_a_refused_announce_leaves_no_peer_object_behind():
    clock = Clock()
    live = FakePeer("http://a/livestack")
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=clock,
                        membership=MembershipPolicy(max_peers=1))
    broker.register_url(live.base, make_peer=lambda _: live)
    other = FakePeer("http://b/livestack")
    with pytest.raises(RosterFull):
        broker.register_url(other.base, make_peer=lambda _: other)
    assert [p.base for p in broker.peers] == [live.base]


def test_a_peer_that_cannot_be_built_leaves_no_roster_row():
    # hostd without meshlink on its path: MeshPeer() raises for a mesh:// announce.
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[], clock=Clock())

    def unbuildable(_url):
        raise RuntimeError("meshlink packages are not importable")
    with pytest.raises(RuntimeError):
        broker.register_url("mesh://livestack/tower-tts/livestack", make_peer=unbuildable)
    assert broker.membership_snapshot() == []
    assert broker.peers == []


def test_pruning_a_seed_drops_its_row_from_the_fleet_view_and_names_why():
    """The field case end to end at the broker: a seed that never answered and a
    registered node gone for the window both leave `/fleet`, a live node stays,
    and the log says which kind of row went and how long it had been unseen."""
    clock = Clock()
    logs = []
    dead_seed = FakePeer("http://127.0.0.1:8100/livestack")
    dead_seed.up = False
    gone = FakePeer("http://100.64.0.18:8190/livestack")
    live = FakePeer("http://100.64.0.18:8100/livestack")
    broker = HostBroker(devices=[Device("gpu0", "h", capacity={"vram": 24})],
                        peers=[dead_seed], clock=clock, log=logs.append,
                        membership=MembershipPolicy(prune_after_s=3600, prune_seeds=True))
    broker.register_url(gone.base, make_peer=lambda _: gone)
    broker.register_url(live.base, make_peer=lambda _: live)
    gone.up = False
    clock.advance(3000)
    broker.roster.mark_seen(live.base)
    clock.advance(1000)
    assert sorted(broker.prune_absent()) == sorted([dead_seed.base, gone.base])
    assert [r["peer"] for r in broker.membership_snapshot()] == [live.base]
    assert [p.base for p in broker.peers] == [live.base]
    pruned = [m for m in logs if "pruned absent peer" in m]
    assert any("127.0.0.1:8100" in m and "seed" in m and "unseen 4000" in m for m in pruned), pruned
    assert any("8190" in m and "registered" in m for m in pruned), pruned


def test_tick_announces_decay_even_when_nothing_probes():
    """State is derived on read but announced on write. If probing stops, the
    roster must still say so — silence is the failure being fixed."""
    clock = Clock()
    lines = []
    roster = PeerRoster(POLICY, clock=clock, log=lines.append)
    roster.seed("p")
    lines.clear()
    clock.advance(700)               # nothing probes at all
    assert not lines
    roster.tick()
    assert len(lines) == 1 and "mia" in lines[0]
    roster.tick()                    # idempotent — still not an event
    assert len(lines) == 1
