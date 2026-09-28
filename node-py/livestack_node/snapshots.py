"""snapshots.py — the exact state `plan()` decided on, stored so it can be re-run.

A ledger row for an eviction said what was evicted and why, and nothing about
the footprints, capacities or measured free memory the planner had in hand, so
nobody could re-run the decision to ask whether it was right.
`scheduler-policy-routine` design §10 names that as the blocker for tuning
placement at all. This stores the `WorldState` (and the `PlannerPolicy`) once
per distinct state, content-addressed, and every action row of that plan points
at it.

Bound (benchday rule 10): the store deletes oldest-first past a byte cap, and
past an age window when one is set, on every write. Identical states dedupe to
one file, so a 5-second reconcile loop that sees nothing change adds nothing.
Stdlib only (gzip), like the rest of the package.
"""
from __future__ import annotations

import dataclasses
import enum
import gzip
import hashlib
import json
import os
import time
from typing import Callable, Optional, Tuple

from .planner import (Defer, Device, Evict, Grant, Load, Placement, PlannerPolicy, Plan,
                      Request, Residency, Unit, WorldState)


def _plain(v):
    if isinstance(v, enum.Enum):
        return int(v.value) if isinstance(v.value, int) else v.value
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (frozenset, set)):
        return sorted(_plain(x) for x in v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict) or hasattr(v, "items"):
        return {str(k): _plain(x) for k, x in sorted(dict(v).items())}
    return v


def world_to_json(world: WorldState, policy: Optional[PlannerPolicy] = None) -> dict:
    return {"world": _plain(world), "policy": _plain(policy) if policy is not None else None}


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def snapshot_hash(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


def world_from_json(payload: dict) -> Tuple[WorldState, Optional[PlannerPolicy]]:
    w = payload["world"]
    units = {}
    for kind, u in w["units"].items():
        units[kind] = Unit(**{**u, "residency": Residency(u["residency"]),
                              "servable_on": frozenset(u.get("servable_on") or ())})
    world = WorldState(
        devices=tuple(Device(**d) for d in w["devices"]),
        units=units,
        placements=tuple(Placement(**p) for p in w["placements"]),
        requests=tuple(Request(**r) for r in w["requests"]),
        now=w["now"],
        last_evicted_at=w["last_evicted_at"],
        demand=w["demand"],
        measured_free=w["measured_free"],
    )
    pol = payload.get("policy")
    return world, (PlannerPolicy(**pol) if pol is not None else None)


def plan_signature(p: Plan) -> list:
    """What the ledger records per action, comparable across a replay: the
    decision, the unit, the device and (for Grant/Defer) the request."""
    out = []
    for a in p.actions:
        name = {Evict: "evict", Load: "load", Grant: "grant", Defer: "defer"}[type(a)]
        out.append((name, getattr(a, "kind", None), getattr(a, "device_id", None),
                    getattr(a, "request_id", None)))
    return sorted(out, key=lambda t: tuple(str(x) for x in t))


class SnapshotStore:
    def __init__(self, root: str, *, max_bytes: int = 512 * 1024 * 1024,
                 max_age_s: Optional[float] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda *_: None) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self._clock = clock
        self._log = log
        self.failed = 0
        os.makedirs(root, exist_ok=True)

    def _path(self, h: str) -> str:
        return os.path.join(self.root, h.split(":", 1)[1] + ".json.gz")

    def put(self, world: WorldState, policy: Optional[PlannerPolicy] = None) -> Optional[str]:
        """Store (or re-touch) this state; return its hash, or None if it could
        not be written. A snapshot is evidence, never worth failing a plan for,
        but a failure is counted and logged rather than hidden."""
        try:
            payload = world_to_json(world, policy)
            h = snapshot_hash(payload)
            path = self._path(h)
            if os.path.exists(path):
                os.utime(path)          # age counts from last use, not first
                return h
            tmp = path + ".tmp"
            with gzip.open(tmp, "wb") as fh:
                fh.write(canonical(payload))
            os.replace(tmp, path)
            self._prune()
            return h
        except Exception as exc:
            self.failed += 1
            self._log(f"[snapshots] could not store plan state: {type(exc).__name__}: {exc}")
            return None

    def put_payload(self, payload: dict) -> Optional[str]:
        """Store any canonical-JSON input a decision was made on (a composition
        run stores the node facts it read). Same hash, dedupe and bound."""
        try:
            h = snapshot_hash(payload)
            path = self._path(h)
            if os.path.exists(path):
                os.utime(path)
                return h
            tmp = path + ".tmp"
            with gzip.open(tmp, "wb") as fh:
                fh.write(canonical(payload))
            os.replace(tmp, path)
            self._prune()
            return h
        except Exception as exc:
            self.failed += 1
            self._log(f"[snapshots] could not store decision input: {type(exc).__name__}: {exc}")
            return None

    def load_payload(self, h: str) -> dict:
        with gzip.open(self._path(h), "rb") as fh:
            return json.loads(fh.read())

    def load(self, h: str) -> Tuple[WorldState, Optional[PlannerPolicy]]:
        with gzip.open(self._path(h), "rb") as fh:
            return world_from_json(json.loads(fh.read()))

    def _prune(self) -> None:
        entries = []
        for name in os.listdir(self.root):
            if not name.endswith(".json.gz"):
                continue
            p = os.path.join(self.root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        entries.sort()
        now = self._clock()
        total = sum(e[1] for e in entries)
        for mtime, size, p in entries:
            too_old = self.max_age_s is not None and now - mtime > self.max_age_s
            if not too_old and total <= self.max_bytes:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass


def snapshot_store_from_env(name: str, log: Callable[[str], None] = lambda *_: None
                            ) -> Optional[SnapshotStore]:
    """Follows the ledger's switches: off with `LIVESTACK_LEDGER=0`, the same
    age window (`LIVESTACK_LEDGER_AGE_DAYS`, unset = no age deletion) and its
    own byte cap (`LIVESTACK_SNAPSHOT_MAX_MB`, default 512)."""
    if os.environ.get("LIVESTACK_LEDGER", "1") == "0":
        return None
    age = os.environ.get("LIVESTACK_LEDGER_AGE_DAYS", "").strip()
    root = os.path.join(os.path.expanduser("~"), ".cache", "livestack", "snapshots", name)
    return SnapshotStore(
        root,
        max_bytes=int(float(os.environ.get("LIVESTACK_SNAPSHOT_MAX_MB", "512")) * 1024 * 1024),
        max_age_s=float(age) * 86400 if age else None,
        log=log,
    )
