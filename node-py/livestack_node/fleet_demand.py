"""fleet_demand.py — what the fleet was asked for and could not give.

`POST /fleet/admit` answers one job and keeps nothing. `fleetTick` plans over a
queue its caller hands it. Nothing joined the two, so a job the broker answered
with `Queue` never reached a plan and a burst the scheduler would have
authorised never happened. This is that join.

**It is a demand SIGNAL, not a work queue, and the difference is the whole
design.**

A queue promises something. It has to be durable, it has to be bounded, and —
the part that actually bites — it can hold work whose caller gave up ten minutes
ago. Provisioning a machine for a request nobody is waiting for spends money for
nothing, and a queue cannot tell the difference between patient demand and
abandoned demand.

A register of *recent, unmet* demand cannot make that mistake:

* Every entry has a TTL. Demand must be CURRENT to justify spending, and a
  caller that stopped asking stops counting within `ttl_s`.
* Nothing is promised to anyone. The caller already got its refusal and is
  responsible for its own retry, exactly as today — attune throws
  `NoFleetTarget`, media-corpus falls back locally. Their retries are what keep
  the signal alive, which means the signal is a measurement of real waiting
  rather than a record of past disappointment.
* It is IN MEMORY on purpose. A broker restart losing it is correct: demand
  older than the restart is not current demand, and reconstructing it from disk
  would resurrect exactly the abandoned requests the TTL exists to forget.

Bounded (rule 10): at most `max_entries`, evicting least-recently-seen, plus the
TTL sweep. Both enforced by this class on every write.

**A quota refusal is never demand.** An account at its ceiling does not need a
bigger fleet, and renting one would not admit its next job. Only a refusal for
lack of capacity is recorded — see :meth:`DemandRegister.record`.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

#: How long a refusal keeps counting as demand. Short on purpose: it is the
#: window in which somebody is plausibly still waiting, not a history.
DEFAULT_TTL_S = 120.0
#: Bound. A fleet with more distinct unmet shapes than this has a bigger problem
#: than bookkeeping, and the oldest is the least likely to still matter.
DEFAULT_MAX_ENTRIES = 256


@dataclass
class Demand:
    """One shape of work the fleet could not take, and how often lately."""
    kind: str
    owner: str
    sla: str
    regions: Tuple[str, ...] = ()
    selector: Mapping[str, str] = field(default_factory=dict)
    est_duration_s: float = 60.0
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_reason: str = ""

    @property
    def key(self) -> Tuple:
        return demand_key(self.kind, self.owner, self.sla, self.regions,
                          self.selector)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "owner": self.owner, "sla": self.sla,
                "regions": list(self.regions), "selector": dict(self.selector),
                "est_duration_s": self.est_duration_s, "count": self.count,
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "last_reason": self.last_reason}


def demand_key(kind: str, owner: str, sla: str, regions, selector) -> Tuple:
    return (kind, owner, sla, tuple(sorted(regions or ())),
            tuple(sorted((selector or {}).items())))


class DemandRegister:
    """Recent unmet demand. Thread-safe, bounded, and forgetful by design."""

    def __init__(self, *, ttl_s: float = DEFAULT_TTL_S,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda *_: None):
        self.ttl_s = float(ttl_s)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._entries: Dict[Tuple, Demand] = {}

    # -- writing -------------------------------------------------------------
    def record(self, *, kind: str, owner: str, sla: str = "normal",
               regions=(), selector: Optional[Mapping[str, str]] = None,
               est_duration_s: float = 60.0, reason: str = "",
               now: Optional[float] = None) -> Optional[Demand]:
        """Note that the fleet could not take this shape of work.

        Returns the entry, or None when the refusal is not demand at all. The
        caller decides what counts by calling this only for a capacity refusal
        — the one thing a bigger fleet would have fixed.
        """
        now = self._clock() if now is None else now
        key = demand_key(kind, owner, sla, regions, selector)
        with self._lock:
            self._expire(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = Demand(kind=kind, owner=owner, sla=sla,
                               regions=tuple(sorted(regions or ())),
                               selector=dict(selector or {}),
                               est_duration_s=float(est_duration_s),
                               first_seen=now)
                self._entries[key] = entry
            entry.count += 1
            entry.last_seen = now
            entry.last_reason = str(reason or "")[:300]
            # The longest estimate anyone asked for: a pool instance has to be
            # able to hold the work, and under-stating that is how a burst lands
            # a job on a machine that cannot finish it inside its deadline.
            entry.est_duration_s = max(entry.est_duration_s, float(est_duration_s))
            self._evict(now)
            return entry

    # -- reading -------------------------------------------------------------
    def live(self, now: Optional[float] = None) -> List[Demand]:
        """Demand still inside its TTL, most-wanted first."""
        now = self._clock() if now is None else now
        with self._lock:
            self._expire(now)
            return sorted(self._entries.values(),
                          key=lambda d: (-d.count, d.first_seen, d.kind))

    def jobs(self, now: Optional[float] = None) -> List[dict]:
        """Live demand as job rows `POST /fleet/plan` understands.

        **One job per shape, never one per refusal.** Forty refusals of the same
        thing in two minutes are evidence that one more worker is wanted, not
        that forty are: a pool instance serves several concurrent jobs, and the
        pool's own `max_instances` plus the next tick are what scale it further.
        Turning a refusal count into a job count is how a brief spike rents a
        datacentre.
        """
        out = []
        for d in self.live(now):
            out.append({
                # `demand:` marks it as synthesised, so a reader never mistakes
                # it for a job some caller is holding open.
                "job_id": f"demand:{d.kind}:{d.owner}:{d.sla}",
                "kind": d.kind, "sla": d.sla,
                "created_at": d.first_seen,
                "est_duration_s": d.est_duration_s,
                "selector": dict(d.selector),
            })
        return out

    def snapshot(self, now: Optional[float] = None) -> dict:
        live = self.live(now)
        return {"ttl_s": self.ttl_s, "max_entries": self.max_entries,
                "entries": [d.to_dict() for d in live]}

    # -- bounds --------------------------------------------------------------
    def _expire(self, now: float) -> None:
        dead = [k for k, d in self._entries.items()
                if now - d.last_seen > self.ttl_s]
        for k in dead:
            del self._entries[k]

    def _evict(self, now: float) -> None:
        over = len(self._entries) - self.max_entries
        if over <= 0:
            return
        # Least-recently-seen first: the oldest silence is the least likely to
        # still have somebody behind it.
        for k, _ in sorted(self._entries.items(),
                           key=lambda kv: kv[1].last_seen)[:over]:
            del self._entries[k]
        self._log(f"[fleet] demand register at its bound ({self.max_entries}); "
                  f"dropped {over} least-recently-seen shape(s)")
