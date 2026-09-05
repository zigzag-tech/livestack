"""hostbroker.py — host-level planning broker.

The planner is a pure function; this is the thin authority that runs it across the
model-server **processes** sharing one host's devices and dispatches the resulting
evict/load actions to the owning process. It is what makes preemption work on a
single host where polyasr, polytts and chipgen are *separate processes* on one GPU
(an in-process Coordinator can only move its own units).

A ``Peer`` is one model server, reached via its ``/livestack`` facade (or any
object satisfying the duck type): it reports the units it can host and what it has
resident/busy, and obeys ``warm``/``evict``. The broker keeps NO durable state —
peers are ground truth and are re-snapshotted on every decision (soft state), so a
broker restart rebuilds from the fleet.

This is the single-host degenerate case of the mesh domain planner; the same
``HostBroker.plan_and_apply`` runs on a federated WorldState once peers span hosts.
"""
from __future__ import annotations

import os
import threading
import time

from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .planner import (
    Device, Placement, Request, Unit, WorldState, PlannerPolicy, plan, Residency,
    Load, Evict, Grant, Defer,
)
from .membership import MembershipPolicy, PeerRoster, RosterFull
from .ledger import Candidate, Decision, JsonlLedger


def _res_max(a: Mapping[str, float], b: Mapping[str, float]) -> Dict[str, float]:
    return {k: max(float(a.get(k, 0.0)), float(b.get(k, 0.0)))
            for k in set(a) | set(b)}


def aggregate_units(per_peer: Mapping[Tuple[str, str], Unit]) -> Dict[str, Unit]:
    """Fold `(kind, peer) -> Unit` reports down to the per-kind map the planner
    consumes.

    The broker used to do this with `units.setdefault(kind, unit)` — first peer
    wins, every later report silently dropped. That is wrong whenever two nodes
    on one host serve the same kind, which is the normal shape here: two polyasr
    processes on xc-tower-ubuntu, one per card. The planner's `Unit` is per-kind
    by design (one resident copy serves unlimited leases), and two engines of a
    kind are two *placements* of it — which `placements` already models. So the
    fix is not to give the planner a second unit; it is to stop losing the
    reports and to fold them conservatively:

    * footprint and activation headroom take the elementwise MAX. Under-reserving
      is the OOM direction, and "whichever peer we happened to probe first" is
      not a defensible way to choose.
    * priority takes the MIN (lower = more important) and residency the most
      pinned tier, for the same reason: the stronger claim wins.
    * `min_resident` takes the MAX — a fleet-wide warm floor is a floor.
    """
    out: Dict[str, Unit] = {}
    for (kind, _peer), unit in sorted(per_peer.items()):
        prev = out.get(kind)
        if prev is None:
            out[kind] = unit
            continue
        out[kind] = Unit(
            kind=kind,
            footprint=_res_max(prev.footprint, unit.footprint),
            priority=min(prev.priority, unit.priority),
            residency=Residency(min(int(prev.residency), int(unit.residency))),
            min_resident=max(prev.min_resident, unit.min_resident),
            reload_cost=max(prev.reload_cost, unit.reload_cost),
            selector=prev.selector or unit.selector,
            min_residency_s=max(prev.min_residency_s, unit.min_residency_s),
            restore_debounce_s=max(prev.restore_debounce_s, unit.restore_debounce_s),
            activation_headroom=_res_max(prev.activation_headroom,
                                         unit.activation_headroom),
        )
    return out


def peer_key(peer) -> str:
    """Stable identity for a peer. A REST peer is identified by its facade URL,
    which is also what makes registration idempotent across node restarts; a
    fake in a test has no URL and falls back to object identity. Same rule
    sweep_leaks already used, lifted so membership and reclaim agree."""
    return getattr(peer, "base", None) or f"peer-{id(peer)}"


class Peer:
    """Duck type for one model-server process. A real impl wraps the peer's
    ``/livestack`` REST facade; tests pass a fake."""
    host_id: str
    device_id: str

    def units(self) -> Mapping[str, Unit]: ...          # pragma: no cover
    def placements(self) -> List[Placement]: ...        # pragma: no cover
    def device_memory(self) -> Optional[Mapping[str, float]]: ...  # pragma: no cover
    def device_capacity(self) -> Optional[Mapping[str, float]]: ...  # pragma: no cover
    def warm(self, kind: str) -> None: ...              # pragma: no cover
    def evict(self, kind: str) -> None: ...             # pragma: no cover


class HostBroker:
    def __init__(self, devices=None, peers: Optional[List[Peer]] = None,
                 policy: Optional[PlannerPolicy] = None,
                 clock: Optional[Callable[[], float]] = None,
                 log: Callable[[str], None] = lambda *_: None,
                 device_config: Optional[dict] = None,
                 default_capacity: Optional[dict] = None,
                 membership: Optional[MembershipPolicy] = None,
                 extra_units: Optional[Mapping[str, Unit]] = None,
                 dispatch: bool = True,
                 ledger: Optional[JsonlLedger] = None,
                 emitter: str = "host-broker",
                 emitter_id: str = "host-broker"):
        # devices: a FIXED list (single-host / tests). If None, devices are
        # DISCOVERED from the peers' reported device_ids (federated / multi-host),
        # each sized from device_config[device_id] or default_capacity. That is the
        # whole of "federation": the same plan() runs over one device or many.
        self.devices = list(devices) if devices is not None else None
        self.peers: List[Peer] = list(peers or [])
        self.policy = policy or PlannerPolicy()
        self._clock = clock
        self._log = log
        self.device_config = device_config or {}
        # Hosted-backend health, keyed by device id. Set by whatever probes the
        # endpoint (hostd's reconcile loop); an unhealthy backend simply stops
        # being a candidate, so demand falls back to the GPU with no other
        # special case in the planner.
        self.hosted_available: Dict[str, bool] = {}
        # Kinds no peer will ever report (e.g. "build" on a peerless BUILD-host
        # broker). Without them, admit() for such a kind defers "unknown kind".
        self.extra_units: Dict[str, Unit] = dict(extra_units or {})
        # The broker's own lease ledger for hosted backends. Peers are ground
        # truth for what they host, but a hosted device HAS no peer — nobody
        # else can say how busy it is, so the broker must, or the planner's
        # concurrency check always sees an empty house. A lease that stops
        # heartbeating expires: a dead leaseholder must not hold capacity.
        self.hosted_leases: Dict[str, dict] = {}
        self.hosted_lease_ttl_s = float(os.environ.get("LIVESTACK_LEASE_TTL_S", "120"))
        self._lease_lock = threading.Lock()     # the reconcile thread snapshots too
        self._lease_seq = 0
        # Leak reclaim bookkeeping: peer -> last attempt, and how often to retry.
        self._last_reclaim: Dict[str, float] = {}
        self.reclaim_interval_s = float(os.environ.get("LIVESTACK_RECLAIM_INTERVAL", "120"))
        self.default_capacity = default_capacity or {"vram_bytes": 24_000_000_000,
                                                     "reserved": 2_000_000_000}
        # Membership: who is on this host, and who has gone. Peers passed to the
        # constructor are SEEDS (an operator said they ought to exist); peers
        # that arrive via register_url() announced themselves and are pruneable.
        # See membership.py and _plans/peer-membership.md.
        # dispatch=False is OBSERVE-ONLY: plan, report, dispatch nothing. It is
        # the whole safety property of a fleet broker — one card, one master.
        # Only the host broker on a machine may warm or evict units on that
        # machine; a second broker with a fleet-wide view would otherwise fight
        # every host broker it can see. Observe-only is proven by ABSENCE (no
        # warm/evict lines over a window), which is why the flag gates dispatch
        # rather than the planning that precedes it: the plan is the product.
        self.dispatch = dispatch
        self.ledger = ledger
        self.emitter = emitter
        self.emitter_id = emitter_id
        # Per peer, an EWMA (0.7 old / 0.3 new) of the wall-clock cost of one
        # snapshot probe. This is the first and cheapest RTT in the system:
        # already paid for by a probe that has to happen anyway, and measured
        # from where the broker sits — which is also its limitation, and what
        # Phase 2's links matrix exists to remove.
        self.probe_ms: Dict[str, float] = {}
        self._capabilities: Dict[str, dict] = {}
        self._capability_at: Dict[str, float] = {}
        self.capability_ttl_s = float(os.environ.get("LIVESTACK_CAPABILITY_TTL", "15"))
        self.roster = PeerRoster(membership, clock=clock or time.monotonic, log=log,
                                 on_transition=self._emit_transition)
        self._last_probe_error: Dict[str, str] = {}
        # (kind, peer) -> Unit, as of the last snapshot. The planner gets the
        # folded per-kind map; this keeps WHO reported what, which the fleet view
        # needs and the folded map cannot express.
        self.peer_units: Dict[Tuple[str, str], Unit] = {}
        for p in self.peers:
            self.roster.seed(peer_key(p))

    def _resolve_devices(self, discovered: dict,
                         measured_caps: Optional[Mapping[str, Mapping[str, float]]] = None) -> list:
        if self.devices is not None:
            return self.devices
        measured_caps = measured_caps or {}
        out = []
        for did, hid in sorted(discovered.items()):
            # Capacity precedence: explicit device_config (operator intent) > MEASURED
            # (the device's real total / recommended working set) > default guess. So a
            # node that reports its true size auto-sizes its budget instead of being
            # clamped by the conservative LIVESTACK_VRAM_GB default.
            if did in self.device_config:
                cap = self.device_config[did]
                vram = cap["vram_bytes"]
                reserved = cap.get("reserved", 0)
            elif did in measured_caps and measured_caps[did].get("vram_bytes"):
                vram = measured_caps[did]["vram_bytes"]
                reserved = self.default_capacity.get("reserved", 0)
            else:
                cap = self.default_capacity
                vram = cap["vram_bytes"]
                reserved = cap.get("reserved", 0)
            out.append(Device(did, hid, capacity={"vram_bytes": vram},
                              reserved={"vram_bytes": reserved}))
        out.extend(self._hosted_devices())
        return out

    def _hosted_devices(self) -> list:
        """Hosted backends declared by the operator, appended to the discovered
        local devices.

        A hosted backend is not discovered — nothing on this host reports it —
        so it can only ever come from configuration. Entries are the
        ``device_config`` rows carrying ``hosted: true``; they are ignored by the
        local-device loop above because no peer discovers their id.
        """
        out = []
        for did, cfg in sorted(self.device_config.items()):
            if not cfg.get("hosted"):
                continue
            capacity = {}
            if cfg.get("concurrency"):
                capacity["concurrency"] = float(cfg["concurrency"])
            out.append(Device(
                did, cfg.get("host_id", "hosted"),
                capacity=capacity,
                labels=cfg.get("labels", {}),
                hosted=True,
                cost_bias=float(cfg.get("cost_bias", 0.0)),
                available=self.hosted_available.get(did, True),
            ))
        return out

    # -- hosted lease ledger (the broker IS the source of truth here) ---------
    def _now(self, now: Optional[float] = None) -> float:
        return now if now is not None else (self._clock() if self._clock else time.time())

    def hosted_checkout(self, device_id: str, kind: str, owner: str,
                        now: Optional[float] = None) -> str:
        """Record a lease taken against a hosted backend; returns its id.

        Called when a grant lands on a hosted device — from that moment the
        device's concurrency budget is one lease tighter, which is exactly what
        keeps a second admit from double-booking a machine that fits one build.
        """
        now = self._now(now)
        with self._lease_lock:
            self._lease_seq += 1
            lease_id = f"{device_id}-{int(now * 1000)}-{self._lease_seq}"
            self.hosted_leases[lease_id] = {"device_id": device_id, "kind": kind,
                                            "owner": owner, "created": now, "last_hb": now}
        return lease_id

    def hosted_heartbeat(self, lease_id: str, now: Optional[float] = None) -> bool:
        """Refresh a lease; False when unknown or already past TTL. Heartbeat is
        the leaseholder's proof of life — without it the ledger would leak
        capacity to every client that died mid-build."""
        now = self._now(now)
        with self._lease_lock:
            lease = self.hosted_leases.get(lease_id)
            if lease is None or now - lease["last_hb"] > self.hosted_lease_ttl_s:
                self.hosted_leases.pop(lease_id, None)
                return False
            lease["last_hb"] = now
            return True

    def hosted_release(self, lease_id: str) -> bool:
        with self._lease_lock:
            return self.hosted_leases.pop(lease_id, None) is not None

    def set_hosted_available(self, device_id: str, available: bool) -> None:
        """Flip the health gate hostd's prober feeds. An unhealthy backend simply
        stops being a candidate; nothing else has to know why."""
        self.hosted_available[device_id] = available

    def _hosted_placements(self, now: Optional[float] = None) -> List[Placement]:
        """Live leases per hosted device, as Placements.

        Expires first: a lease older than the TTL is a dead leaseholder, and
        dead capacity must come back on its own — clients that crash cannot be
        relied on to release. The survivor count feeds the planner's
        ``_hosted_has_room`` on the next snapshot.
        """
        now = self._now(now)
        with self._lease_lock:
            for lid in [lid for lid, l in self.hosted_leases.items()
                        if now - l["last_hb"] > self.hosted_lease_ttl_s]:
                del self.hosted_leases[lid]
            live: Dict[str, Dict[str, int]] = {}
            for l in self.hosted_leases.values():
                live.setdefault(l["device_id"], {})
                live[l["device_id"]][l["kind"]] = live[l["device_id"]].get(l["kind"], 0) + 1
        return [Placement(kind, did, leases=n)
                for did, kinds in live.items() for kind, n in kinds.items()]

    def register_peer(self, peer: Peer) -> None:
        self.peers.append(peer)
        self.roster.seed(peer_key(peer))

    def register_url(self, facade_url: str, make_peer: Callable[[str], Peer],
                     **meta) -> dict:
        """A node announcing itself. Idempotent on the facade URL, so a node
        that restarts re-registers rather than duplicating, and a broker that
        restarts refills from the nodes within one heartbeat.

        This is the path that makes starting a model server the ONLY action
        required: the node knows its own facade URL because it is serving it,
        and the broker's address is a host constant with a working default.
        """
        key = facade_url.rstrip("/")
        existing = next((p for p in self.peers if peer_key(p) == key), None)
        if existing is None:
            self.peers.append(make_peer(key))
        rec = self.roster.register(key, **meta)
        return {"peer": key, "source": rec.source,
                "state": self.roster.state_of(key),
                "peers": len(self.peers)}

    def prune_absent(self) -> List[str]:
        """Forget registered peers that have been gone past the prune window.
        Seeds are never pruned and an unset window disables this entirely, so
        the roster shrinks only where shrinking is provably safe: a registered
        peer re-announces the moment it returns."""
        gone = self.roster.prunable()
        for key in gone:
            self.peers = [p for p in self.peers if peer_key(p) != key]
            self.roster.drop(key)
            self._last_probe_error.pop(key, None)
            self._log(f"[membership] pruned absent peer: {key}")
        return gone

    def membership_snapshot(self) -> List[dict]:
        """Roster with per-peer state, age, and the last probe error — so
        'is my TTS node gone, or did it just blip?' is answerable without
        grepping a log."""
        out = self.roster.snapshot()
        for row in out:
            err = self._last_probe_error.get(row["peer"])
            if err and row["state"] != "fresh":
                row["last_error"] = err
            ms = self.probe_ms.get(row["peer"])
            if ms is not None:
                row["probe_ms"] = round(ms, 1)
        return out

    def sweep_leaks(self, now: Optional[float] = None) -> list:
        """Ask any peer reporting a leak to return its allocator pool.

        Runs BEFORE planning, because reclaimed memory changes what fits — a
        plan built against a card that is about to gain 14 GB would evict things
        it does not need to.

        Throttled per peer: reclaim touches the GPU executor, and hammering it
        would contend with real work. A node whose pool does not come back is
        reporting a genuine leak rather than a stale cache, and that is worth
        seeing in the log rather than retried in a tight loop.
        """
        now = now if now is not None else time.time()
        acted = []
        for peer in self.peers:
            try:
                leak = getattr(peer, "leak", None)
                if not leak:
                    continue
                key = getattr(peer, "base", str(id(peer)))
                if now - self._last_reclaim.get(key, 0.0) < self.reclaim_interval_s:
                    continue
                self._last_reclaim[key] = now
                result = peer.reclaim() or {}
                freed = int(result.get("freed_bytes", 0))
                acted.append({"peer": key, "freed_bytes": freed,
                              "unexplained_bytes": int(leak.get("unexplained_bytes", 0))})
                print(f"[harmony] reclaim peer={key} unexplained="
                      f"{int(leak.get('unexplained_bytes', 0)) / 1e9:.1f}GB "
                      f"freed={freed / 1e9:.1f}GB", flush=True)
            except Exception as exc:
                print(f"[harmony] reclaim failed peer={getattr(peer, 'base', '?')}: {exc}", flush=True)
        return acted

    # -- world assembly (soft state: re-snapshot peers every time) ------------
    def snapshot(self, requests: Optional[List[Request]] = None,
                 last_evicted_at: Optional[Mapping[str, float]] = None) -> WorldState:
        # Keyed by (kind, peer) so two nodes serving one kind on one host are
        # both recorded rather than the second being dropped by a setdefault.
        # Folded to the planner's per-kind map by aggregate_units() below.
        per_peer_units: Dict[Tuple[str, str], Unit] = {}
        placements: List[Placement] = []
        discovered: Dict[str, str] = {}     # device_id -> host_id (federated discovery)
        measured: Dict[str, Dict[str, float]] = {}        # device_id -> measured free
        measured_caps: Dict[str, Dict[str, float]] = {}   # device_id -> measured capacity
        for p in self.peers:
            key = peer_key(p)
            # Backoff: a peer already known absent is probed on the roster's slow
            # cadence, not on every reconcile tick. This is what makes holding a
            # peer that is not there free rather than costly — and it is why
            # seeds can be kept indefinitely without paying a connect per cycle.
            if not self.roster.due_for_probe(key):
                continue
            probe_started = time.monotonic()
            try:
                peer_units = p.units()
                peer_placements = p.placements()
            except Exception as e:
                # A peer that is down/slow/erroring must NOT blind the whole arbiter.
                # Skip it and plan over the survivors: the surviving peers' driver-level
                # measured_free still reflects this peer's VRAM (mem_get_info counts all
                # processes on the card), so co-resident eviction stays safe instead of
                # snapshot aborting -> the caller fail-opening -> OOM.
                #
                # The failure is recorded, NOT logged here: printing per cycle is
                # what produced 92,089 identical lines and 9.2 MB of log while one
                # peer was down. mark_probed logs the fresh→suspect→mia
                # transitions, which is where the information actually is.
                self._last_probe_error[key] = str(e)
                self.roster.mark_probed(key)
                continue
            self._record_probe_ms(key, (time.monotonic() - probe_started) * 1000.0)
            self.roster.mark_seen(key)
            for kind, unit in peer_units.items():
                per_peer_units[(kind, key)] = unit
            placements.extend(peer_placements)
            try:
                discovered[p.device_id] = p.host_id
                # Backfill the roster from what the peer actually reports. A
                # registration states only where to reach the node; the device
                # it occupies is the node's own fact, learned by asking it.
                rec = self.roster._records.get(key)
                if rec is not None:
                    rec.device_id, rec.host_id = p.device_id, p.host_id
            except Exception:
                pass
            try:
                mem = p.device_memory()
                if mem:
                    measured[p.device_id] = mem
            except Exception:
                pass
            try:
                cap = p.device_capacity()
                if cap:
                    measured_caps[p.device_id] = cap
            except Exception:
                pass
        now = self._clock() if self._clock else 0.0
        # What each peer said, kept for the fleet view and for anyone asking
        # "which node reported this kind" — a question the folded map cannot
        # answer.
        self.peer_units = per_peer_units
        units = aggregate_units(per_peer_units)
        # Config-declared kinds (a hosted backend has no peer to report them).
        # Peers stay authoritative for the kinds they DO report.
        for kind, u in self.extra_units.items():
            units.setdefault(kind, u)
        # A hosted device has no peer reporting placements either — the broker's
        # own ledger is the only account of how full it is.
        placements.extend(self._hosted_placements(now))
        return WorldState(devices=tuple(self._resolve_devices(discovered, measured_caps)),
                          units=units,
                          placements=tuple(placements), requests=tuple(requests or ()),
                          now=now, last_evicted_at=dict(last_evicted_at or {}),
                          measured_free=measured)

    # -- dispatch -------------------------------------------------------------
    def _peer_for(self, kind: str, device_id: str) -> Optional[Peer]:
        for p in self.peers:
            try:
                if p.device_id == device_id and kind in p.units():
                    return p
            except Exception:
                # A down/erroring peer must not break dispatch of an action bound for
                # a healthy peer sharing the same device — skip it.
                continue
        return None

    def plan_and_apply(self, requests: Optional[List[Request]] = None,
                       last_evicted_at: Optional[Mapping[str, float]] = None):
        """Snapshot the fleet, plan, and dispatch every action to the owning peer.
        Evicts are applied before loads so VRAM is freed first. Returns the Plan."""
        world = self.snapshot(requests, last_evicted_at)
        p = plan(world, self.policy)
        self._emit_plan(world, p)
        if not self.dispatch:
            # Observe-only. The plan is still computed, recorded and returned —
            # that is the product of a fleet broker — but nothing is sent to a
            # peer. One card, one master: only the host broker on a machine may
            # warm or evict units on that machine.
            return p
        for ev in p.of(Evict):
            peer = self._peer_for(ev.kind, ev.device_id)
            if peer is not None:
                self._log(f"[hostbroker] evict {ev.kind}@{ev.device_id}: {ev.reason}")
                peer.evict(ev.kind)
        for ld in p.of(Load):
            peer = self._peer_for(ld.kind, ld.device_id)
            if peer is not None:
                self._log(f"[hostbroker] warm {ld.kind}@{ld.device_id}: {ld.reason}")
                peer.warm(ld.kind)
        return p


    # -- distance ------------------------------------------------------------
    def _record_probe_ms(self, key: str, ms: float) -> None:
        """EWMA of one peer's snapshot cost, 0.7 old / 0.3 new.

        This is the cheapest possible distance signal: the probe has to happen
        anyway, so measuring it costs nothing and it is the only latency number
        the fleet has before Phase 2's links matrix. Its limitation is stated
        rather than hidden — it is distance FROM WHERE THIS BROKER SITS, so a
        client in Nanjing must not be ranked by Vaughan's view of Nanjing.
        """
        prev = self.probe_ms.get(key)
        self.probe_ms[key] = ms if prev is None else prev * 0.7 + ms * 0.3

    # -- the fleet view ------------------------------------------------------
    def _capability_of(self, peer, key: str, now: float) -> Optional[dict]:
        """One node's readiness descriptor, cached for `capability_ttl_s`.

        Cached because `/fleet` is a poll surface: without a TTL, a UI ticking at
        1 Hz would probe every node in the fleet every second, and probes to
        Nanjing cost 0.5-1.5 s each. Soft state, like everything else here — a
        cache miss just costs one probe.
        """
        at = self._capability_at.get(key)
        if at is not None and now - at < self.capability_ttl_s:
            return self._capabilities.get(key)
        cap = getattr(peer, "capability", None)
        if cap is None:
            return self._capabilities.get(key)
        try:
            got = cap()
        except Exception as e:
            self._last_probe_error[key] = str(e)
            self._capability_at[key] = now
            # Keep the LAST known descriptor rather than blanking the row: the
            # row must still say what this node is, and `state`/`last_error`
            # already say that it cannot be read right now.
            return self._capabilities.get(key)
        self._capabilities[key] = got
        self._capability_at[key] = now
        return got

    def fleet_view(self) -> dict:
        """Every node the broker knows, grouped by host — the whole-fleet view.

        The one rule that shapes it: **an absence is a row, never a gap.** A peer
        that cannot be read this instant still appears, with its state, its age
        and its last error. A view that silently omits what it cannot reach is
        the same failure as a roster that cannot go stale: it can only report
        health, so it cannot report the thing you opened it to find out.

        `probe_ms` is absent until measured, and `load` is absent when the node
        reports none — absent means NO OPINION, and a consumer must never read it
        as idle.
        """
        now = time.time()
        rows_by_host: Dict[str, List[dict]] = {}
        by_key = {peer_key(p): p for p in self.peers}
        for row in self.membership_snapshot():
            key = row["peer"]
            peer = by_key.get(key)
            cap = self._capability_of(peer, key, now) if peer is not None else None
            node = {
                "peer": key,
                "source": row.get("source"),
                "state": row["state"],
                "unseen_seconds": row["unseen_seconds"],
                "device_id": row.get("device_id"),
                "kinds": row.get("kinds") or [],
            }
            if key in self.probe_ms:
                node["probe_ms"] = round(self.probe_ms[key], 1)
            if row.get("last_error"):
                node["last_error"] = row["last_error"]
            if cap:
                node["ready"] = cap.get("ready")
                node["detail"] = cap.get("detail")
                if cap.get("device_id"):
                    node["device_id"] = cap["device_id"]
                if cap.get("units"):
                    node["kinds"] = node["kinds"] or [cap.get("kind")]
                if cap.get("labels"):
                    node["labels"] = cap["labels"]
                if isinstance(cap.get("load"), dict):
                    node["load"] = cap["load"]
                    dev = cap["load"].get("device")
                    if isinstance(dev, dict):
                        node["device_mem"] = dev
            units = [
                {"kind": kind, "priority": u.priority,
                 "residency": int(u.residency),
                 "footprint": dict(u.footprint)}
                for (kind, pk), u in sorted(self.peer_units.items()) if pk == key
            ]
            if units:
                node["units"] = units
            host = (cap or {}).get("host_id") or row.get("host_id") or "unknown"
            rows_by_host.setdefault(host, []).append(node)
        return {
            "dispatch": self.dispatch,
            "peers": len(self.peers),
            "generated_at": now,
            "hosts": {h: {"nodes": sorted(n, key=lambda r: r["peer"])}
                      for h, n in sorted(rows_by_host.items())},
        }

    # -- the decision ledger -------------------------------------------------
    def _emit(self, decision: Decision) -> None:
        if self.ledger is None:
            return
        self.ledger.append(decision)

    def _emit_transition(self, rec, old_state: str, new_state: str) -> None:
        """One `observe` record per membership TRANSITION — never per tick.

        A per-tick emitter is how a 92,089-line log happened, and membership
        already learned that lesson: `fresh->suspect`, `suspect->mia` and
        `mia->fresh` are events, "still gone" is not. Riding the same edge the
        log line rides is what makes that structural rather than remembered.
        """
        if self.ledger is None:
            return
        self._emit(Decision(
            emitter=self.emitter, emitter_id=self.emitter_id,
            decision="observe",
            candidates=[Candidate(
                id=rec.key, host_id=rec.host_id, device_id=rec.device_id,
                state=new_state,
                distance_ms=self.probe_ms.get(rec.key),
                inputs_at=time.time(),
                outcome="ranked",
                reason=f"membership: {old_state} -> {new_state}",
            )],
            chosen=None,
            reason=(f"{rec.key} {old_state} -> {new_state} "
                    f"(unseen {rec.age(self.roster._clock()):.0f}s"
                    + (f", last_error={self._last_probe_error[rec.key]}"
                       if self._last_probe_error.get(rec.key) else "")
                    + ")"),
        ))

    def _emit_plan(self, world: WorldState, p) -> None:
        """One record per Evict/Load/Defer/Grant a plan produced.

        Rate is bounded by the plan, not by the clock: a reconcile tick that
        decides nothing writes nothing. `reason` is the planner's own string —
        those were already good — and what was missing is the CANDIDATE ROWS
        around them. `[hostbroker] evict llm@…: relieve measured over-budget
        pressure` says what happened; it never said what else was possible, so a
        reader could not tell a correct eviction from a wrong one.
        """
        if self.ledger is None:
            return
        actions = list(p.actions)
        if not actions:
            return
        by_device: Dict[str, List[Placement]] = {}
        for pl in world.placements:
            by_device.setdefault(pl.device_id, []).append(pl)
        for a in actions:
            device_id = getattr(a, "device_id", None)
            kind = getattr(a, "kind", None)
            decision = {"Evict": "evict", "Load": "load",
                        "Grant": "grant", "Defer": "defer"}[type(a).__name__]
            cands: List[Candidate] = []
            for other_kind, unit in sorted(world.units.items()):
                resident_here = any(pl.kind == other_kind for pl in
                                    by_device.get(device_id or "", []))
                busy = any(pl.kind == other_kind and pl.busy for pl in
                           by_device.get(device_id or "", []))
                if other_kind == kind:
                    outcome, why = "chosen", (getattr(a, "reason", "") or decision)
                else:
                    outcome = "ranked"
                    why = (f"prio {unit.priority}, tier {int(unit.residency)}, "
                           f"{'resident' if resident_here else 'not resident'}"
                           + (", busy" if busy else ""))
                cands.append(Candidate(
                    id=other_kind, device_id=device_id,
                    host_id=next((d.host_id for d in world.devices
                                  if d.id == device_id), None),
                    resident=resident_here, inputs_at=world.now or time.time(),
                    outcome=outcome, reason=why,
                ))
            free = dict(world.measured_free.get(device_id or "", {}) or {})
            self._emit(Decision(
                emitter=self.emitter, emitter_id=self.emitter_id,
                kind=kind, decision=decision, candidates=cands,
                chosen=kind,
                reason=(getattr(a, "reason", "") or decision)
                       + (f"; measured free {free}" if free else ""),
                request=({"owner": getattr(a, "request_id", None)}
                         if hasattr(a, "request_id") else None),
            ))

    def admit(self, request: Request,
              last_evicted_at: Optional[Mapping[str, float]] = None) -> Optional[str]:
        """Plan for one new lease request; return the granted device id, or None if
        it was deferred (caller retries / time-multiplexes)."""
        p = self.plan_and_apply([request], last_evicted_at)
        for g in p.of(Grant):
            if g.request_id == request.id:
                return g.device_id
        return None


import json as _json
import urllib.request as _urlreq


def _http(url, body=None, timeout=5):
    data = _json.dumps(body).encode() if body is not None else None
    method = "POST" if body is not None else "GET"
    req = _urlreq.Request(url, data=data,
                          headers={"Content-Type": "application/json"}, method=method)
    with _urlreq.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return _json.loads(raw) if raw else {}


_RES_TO_PRIO = {0: 10, 1: 20, 2: 30}


class RestPeer:
    """Peer backed by a livestack node's /livestack REST facade (GET /residence,
    POST /model/warm, POST /model/evict). Priority is derived from the residency
    tier unless overridden. One snapshot per planning cycle."""

    def __init__(self, base_url, priority_for=None, priorities=None, footprints=None):
        self.base = base_url.rstrip("/")
        self._prio = priority_for or (lambda r: _RES_TO_PRIO.get(r, 100))
        self._priorities = priorities or {}   # explicit kind->priority overrides
        self._footprints = footprints or {}   # explicit kind->vram_bytes overrides
        self._snap = None

    def refresh(self):
        self._snap = _http(f"{self.base}/residence")
        return self._snap

    @property
    def leak(self):
        """The node's own report that it holds VRAM its resident units do not
        explain (see meters.leak_signal). None on a healthy node."""
        return (self._s() or {}).get("leak")

    def reclaim(self):
        """Ask the node to hand its allocator pool back to the driver.

        The broker cannot do this itself: the memory belongs to the peer's
        process, and only that process can release it. This is the lever that
        was missing when a node reported everything evicted and still held the
        card — detection without it still needs a human.
        """
        # `body={}` is what makes this a POST (see `_http`). It previously
        # passed `method="POST", body={}` as keywords, which `_http` does not
        # accept — so the one lever that recovers a leaked allocator pool raised
        # TypeError every time it was pulled, inside the broker's `except`.
        return _http(f"{self.base}/model/reclaim", {})

    def _s(self):
        return self._snap or self.refresh()

    @property
    def host_id(self):
        return self._s()["host_id"]

    @property
    def device_id(self):
        return self._s()["device_id"]

    def units(self):
        snap = self.refresh()
        out = {}
        for u in snap["units"]:
            r = u["residency"]
            prio = self._priorities.get(u["kind"], self._prio(r))
            fp = ({"vram_bytes": self._footprints[u["kind"]]}
                  if u["kind"] in self._footprints else u["footprint"])
            # Measured peak-activation reserve (absent on nodes that don't report it).
            hdrm = u.get("activation_headroom") or {}
            out[u["kind"]] = Unit(u["kind"], fp, priority=prio,
                                  residency=Residency(r), activation_headroom=hdrm)
        return out

    def placements(self):
        snap = self._s()
        return [Placement(u["kind"], snap["device_id"], busy=u["busy"])
                for u in snap["units"] if u["resident"]]

    def device_memory(self):
        """Measured free resource vector (e.g. {"vram_bytes": ...}) the node read off
        its device this snapshot, or None if the node reports no live meter."""
        dev = self._s().get("device_mem")
        if not dev or not dev.get("free"):
            return None
        return dict(dev["free"])

    def device_capacity(self):
        """Measured device capacity (real total / recommended working set), or None."""
        dev = self._s().get("device_mem")
        if not dev or not dev.get("capacity"):
            return None
        return dict(dev["capacity"])

    def capability(self):
        """The node's readiness descriptor, UNCACHED.

        Deliberately not folded into the `_snap` cache that `/residence` uses:
        `/capability` is the surface carrying `ready`, `detail` and `load`, and
        load computed at read time is the entire contract (see facade.py). A
        cached load report would say an engine is idle while it is saturated,
        which is worse than saying nothing. The caching that `/fleet` needs is
        a TTL at the BROKER, where the poll rate is known.
        """
        return _http(f"{self.base}/capability")

    def warm(self, kind):
        _http(f"{self.base}/model/warm", {"unit": kind}, timeout=180)

    def evict(self, kind):
        _http(f"{self.base}/model/evict", {"unit": kind}, timeout=60)
