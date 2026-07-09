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

from typing import Callable, Dict, List, Mapping, Optional

from .planner import (
    Device, Placement, Request, Unit, WorldState, PlannerPolicy, plan, Residency,
    Load, Evict, Grant, Defer,
)


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
                 default_capacity: Optional[dict] = None):
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
        self.default_capacity = default_capacity or {"vram_bytes": 24_000_000_000,
                                                     "reserved": 2_000_000_000}

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
        return out

    def register_peer(self, peer: Peer) -> None:
        self.peers.append(peer)

    # -- world assembly (soft state: re-snapshot peers every time) ------------
    def snapshot(self, requests: Optional[List[Request]] = None,
                 last_evicted_at: Optional[Mapping[str, float]] = None) -> WorldState:
        units: Dict[str, Unit] = {}
        placements: List[Placement] = []
        discovered: Dict[str, str] = {}     # device_id -> host_id (federated discovery)
        measured: Dict[str, Dict[str, float]] = {}        # device_id -> measured free
        measured_caps: Dict[str, Dict[str, float]] = {}   # device_id -> measured capacity
        for p in self.peers:
            try:
                peer_units = p.units()
                peer_placements = p.placements()
            except Exception as e:
                # A peer that is down/slow/erroring must NOT blind the whole arbiter.
                # Skip it and plan over the survivors: the surviving peers' driver-level
                # measured_free still reflects this peer's VRAM (mem_get_info counts all
                # processes on the card), so co-resident eviction stays safe instead of
                # snapshot aborting -> the caller fail-opening -> OOM.
                self._log(f"[hostbroker] peer unreachable, skipping this cycle: {e}")
                continue
            for kind, unit in peer_units.items():
                units.setdefault(kind, unit)
            placements.extend(peer_placements)
            try:
                discovered[p.device_id] = p.host_id
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

    def warm(self, kind):
        _http(f"{self.base}/model/warm", {"unit": kind}, timeout=180)

    def evict(self, kind):
        _http(f"{self.base}/model/evict", {"unit": kind}, timeout=60)
