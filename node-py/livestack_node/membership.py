"""membership.py — who is on this host, and who has gone.

The broker's third pure brain. `planner.py` decides what should be resident;
this decides *whose reports count* in the first place. Same discipline: no I/O,
no ambient clock — time is passed in — so the whole state machine is testable
without a socket.

The problem it exists for: `HostBroker` took a static peer list, so a node that
existed was invisible until an operator edited `LIVESTACK_PEERS`, and a node
that had gone was polled forever with one log line per cycle and no record of
how long it had been absent. On xc-mac-studio that produced 92,089 identical
"peer unreachable" lines and 9.2 MB of log while Harmony arbitrated a single
peer and reported nothing wrong. See `_plans/peer-membership.md`.

The north star is that starting a model server is the only action required, so
`LIVESTACK_PEERS` survives here only as *seeds* — never as the way membership
is meant to work.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# A peer learned from configuration. The operator said it ought to be here, so
# absence is worth reporting and the entry is never pruned — deleting it would
# silently undo that statement.
SOURCE_SEED = "seed"
# A peer that announced itself. It will announce again when it returns, so
# pruning costs nothing and is what keeps the roster bounded.
SOURCE_REGISTERED = "registered"

FRESH = "fresh"
SUSPECT = "suspect"
MIA = "mia"


@dataclass(frozen=True)
class MembershipPolicy:
    """Thresholds. Ages are elapsed seconds from an injected clock, never a
    count of cycles — a stalled event loop must not manufacture absence."""

    suspect_after_s: float = 45.0
    mia_after_s: float = 600.0
    # None ⇒ pruning DISABLED. An unset window must never mean "delete on the
    # next deploy"; that is the rule a delete-shaped bound has to obey.
    prune_after_s: Optional[float] = None
    max_peers: int = 32
    # Probe cadence by state. Backoff is what makes tolerating absent peers free
    # rather than costly, which is in turn what lets seeds be kept forever.
    fresh_probe_every_s: float = 0.0      # 0 ⇒ every cycle
    suspect_probe_every_s: float = 30.0
    mia_probe_every_s: float = 120.0


def classify(age_s: float, policy: MembershipPolicy) -> str:
    """Age → state. The only place the thresholds are compared."""
    if age_s >= policy.mia_after_s:
        return MIA
    if age_s >= policy.suspect_after_s:
        return SUSPECT
    return FRESH


def probe_interval(state: str, policy: MembershipPolicy) -> float:
    if state == MIA:
        return policy.mia_probe_every_s
    if state == SUSPECT:
        return policy.suspect_probe_every_s
    return policy.fresh_probe_every_s


@dataclass
class PeerRecord:
    key: str
    source: str
    last_seen: float
    registered_at: float
    last_probe: float = field(default=-1e9)
    host_id: Optional[str] = None
    device_id: Optional[str] = None
    kinds: List[str] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    # The state last REPORTED, so transitions can be detected and logged once
    # instead of once per cycle. "still gone" is not an event.
    reported_state: str = FRESH

    def age(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def state(self, now: float, policy: MembershipPolicy) -> str:
        return classify(self.age(now), policy)


class RosterFull(Exception):
    """Registration refused under a named reason rather than growing."""


class PeerRoster:
    """Membership state for one host's peers. Pure apart from the injected
    clock and the log callback."""

    def __init__(self, policy: Optional[MembershipPolicy] = None,
                 clock: Optional[Callable[[], float]] = None,
                 log: Callable[[str], None] = lambda *_: None):
        self.policy = policy or MembershipPolicy()
        self._clock = clock or time.monotonic
        self._log = log
        self._records: Dict[str, PeerRecord] = {}

    # -- population ----------------------------------------------------------

    def seed(self, key: str, **meta) -> PeerRecord:
        return self._upsert(key, SOURCE_SEED, **meta)

    def register(self, key: str, **meta) -> PeerRecord:
        return self._upsert(key, SOURCE_REGISTERED, **meta)

    def _upsert(self, key: str, source: str, **meta) -> PeerRecord:
        now = self._clock()
        rec = self._records.get(key)
        if rec is None:
            if len(self._records) >= self.policy.max_peers:
                raise RosterFull(
                    f"roster is full ({self.policy.max_peers} peers); refusing {key}")
            rec = PeerRecord(key=key, source=source, last_seen=now, registered_at=now)
            self._records[key] = rec
            self._log(f"[membership] {source} peer joined: {key}")
        else:
            # A seed that later self-registers stays a seed: the operator's
            # statement that it ought to exist outlives the node's own report.
            if rec.source != SOURCE_SEED:
                rec.source = source
        for k, v in meta.items():
            if v is not None and hasattr(rec, k):
                setattr(rec, k, v)
        # An announce REGISTERS; only a snapshot CERTIFIES.
        #
        # This used to call mark_seen(key) — "registration IS proof of life" —
        # and that sentence is true of the socket and false of the service.
        # `attach()` starts the registrar thread at import, before the server
        # binds, so a process that never becomes able to serve still announces
        # every 30 s and the renewal reset its age each time. Measured on
        # xc-mac-studio 2026-09-05: polyasr was dead for 7 hours and listed
        # `fresh` throughout, because nothing but the announce was ever needed
        # to keep it there.
        #
        # A NEW record still gets `last_seen=now` at creation above — one grace
        # window of `suspect_after_s` in which to be snapshotted. A RENEWAL now
        # advances nothing, so the only path back to `fresh` is
        # `HostBroker.snapshot()` succeeding against the facade, which already
        # calls mark_seen. Seeds are unaffected; they were never certified by an
        # announce in the first place.
        return rec

    def drop(self, key: str) -> None:
        self._records.pop(key, None)

    # -- liveness ------------------------------------------------------------

    def mark_seen(self, key: str) -> None:
        """A successful snapshot — the ONLY certification of life. Any single
        success returns a peer to fresh immediately: for *absence*, membership is
        a report, not a promise. For *presence* it is the other way round —
        presence needs proof, and an announce is not one (see `_upsert`)."""
        rec = self._records.get(key)
        if rec is None:
            return
        now = self._clock()
        rec.last_seen = now
        rec.last_probe = now
        self._note_transition(rec, FRESH)

    def mark_probed(self, key: str) -> None:
        """A probe was attempted and failed. Records the attempt so backoff
        works, WITHOUT advancing last_seen — a failure is not evidence of life."""
        rec = self._records.get(key)
        if rec is None:
            return
        rec.last_probe = self._clock()
        self._note_transition(rec, rec.state(self._clock(), self.policy))

    def _note_transition(self, rec: PeerRecord, new_state: str) -> None:
        if new_state == rec.reported_state:
            return
        old = rec.reported_state
        rec.reported_state = new_state
        if new_state == FRESH:
            self._log(f"[membership] {rec.key} is back ({old} → fresh)")
        else:
            self._log(f"[membership] {rec.key} {old} → {new_state} "
                      f"(unseen {rec.age(self._clock()):.0f}s)")

    def tick(self) -> None:
        """Re-evaluate every peer and log whatever changed.

        Without this, state is derived on READ but announced only on WRITE, so a
        peer could sit at `mia` in `GET /peers` having never produced a log line
        — and if probing stopped altogether, the whole roster could rot in
        silence. That is the exact failure this module exists to end, so the
        sweep cannot depend on probes still happening.
        """
        now = self._clock()
        for rec in list(self._records.values()):
            self._note_transition(rec, rec.state(now, self.policy))

    # -- reads ---------------------------------------------------------------

    def due_for_probe(self, key: str) -> bool:
        rec = self._records.get(key)
        if rec is None:
            return True
        now = self._clock()
        interval = probe_interval(rec.state(now, self.policy), self.policy)
        return (now - rec.last_probe) >= interval

    def state_of(self, key: str) -> str:
        rec = self._records.get(key)
        if rec is None:
            return MIA
        return rec.state(self._clock(), self.policy)

    def snapshot(self) -> List[dict]:
        now = self._clock()
        return [
            {
                "peer": r.key,
                "source": r.source,
                "state": r.state(now, self.policy),
                "unseen_seconds": round(r.age(now), 1),
                "host_id": r.host_id,
                "device_id": r.device_id,
                "kinds": list(r.kinds),
                "readiness": dict(r.readiness),
            }
            for r in sorted(self._records.values(), key=lambda r: r.key)
        ]

    # -- bounds --------------------------------------------------------------

    def prunable(self) -> List[str]:
        """Registered peers gone long enough to forget. Seeds are never
        returned, and an unset window disables pruning entirely."""
        window = self.policy.prune_after_s
        if window is None:
            return []
        now = self._clock()
        return [r.key for r in self._records.values()
                if r.source == SOURCE_REGISTERED and r.age(now) >= window]
