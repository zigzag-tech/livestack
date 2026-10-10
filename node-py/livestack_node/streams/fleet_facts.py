"""`harmony.fleet/1` and `harmony.jobs/1` bodies, derived from the broker's own views.

`harmony.fleet/1` is governed (one-per-realm): one body per host, the daemon stamps the subject.
The body is the catalog's `harmony_fleet_1`. It is derived from `GET /fleet` and `GET /status` exactly
as the Stage 1 adapter (benchday plugins-v2/harmony.fleet-source, `fleet_body`) derives it; the body
builder `_body` below is a line-for-line port of that function, and `adapter_body` exposes the port so
the parity test can pin it.

READ THIS BEFORE TRUSTING PARITY ON A LIVE BROKER. The adapter reads FLAT keys (`fleet.units`,
`fleet.devices`, `fleet.degraded`, `fleet.demand`, `status.kinds`, `status.node_state`) that the real
broker does not emit: `/fleet` nests rows as `hosts.<host>.nodes[]` and `/status` has `peers[]`
(each with `units[]`, `device_mem`) and a LIST `membership`. Against a live Harmony the adapter therefore
yields empty units/devices and `membership_state: suspect` (its `is_object()` test is false for a list).
`normalise` below maps the real shape onto the flat keys the body builder reads, and passes a view that
is already flat through untouched, so the two agree on the adapter's input and differ (correctly) on the
broker's real output. Deviations from the adapter, all deliberate:

* `host_id` (a schema field the adapter never sets) is the broker's vantage host;
* the three rings carry one zero bucket (the schema requires `counts` minItems 1; the adapter's empty
  array fails validation) and the broker keeps no completion counters yet (design's "upstream gap"), so
  `degraded` names `counters_unavailable` rather than letting a zero read as measured (rule 13);
* `node_state` and `membership_state` are derived from the nodes' membership rows, not from absent keys.

`harmony.jobs/1` (multi-subject, plain `publish`, key `job`) maps workload-authority jobs onto the
catalog body; see `job_body`.

Standard library only; relative imports only.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .common import bounded

RESIDENCY = {0: "HARD_PIN", 1: "SOFT_PIN", 2: "UNPINNED"}
RING_WIDTH_S = 60


def _ring(counts: List[int], start_s: int = 0) -> dict:
    return {"width_s": RING_WIDTH_S, "start_s": start_s, "counts": counts, "since_s": 0}


def _truncated(items: List[Any], cap: int) -> Tuple[List[Any], int]:
    return items[:cap], max(0, len(items) - cap)


def _u64(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _body(fleet: dict, status: dict, membership_state: str, ring_counts: Optional[List[int]] = None,
          start_s: int = 0, extra_degraded: Iterable[str] = ()) -> dict:
    """Port of the adapter's `fleet_body` (guest/src/lib.rs). `ring_counts=[]` reproduces it exactly."""
    units: List[dict] = []
    for unit in fleet.get("units") or []:
        units.append({
            "name": bounded(unit.get("name", ""), 128), "kind": bounded(unit.get("kind", ""), 128),
            "residency": unit.get("residency") if isinstance(unit.get("residency"), str) else "UNPINNED",
            "resident": unit.get("resident") is True, "busy": unit.get("busy") is True,
            "vram_bytes": _u64(unit.get("vram_bytes")),
        })
    units, units_truncated = _truncated(units, 24)
    devices: List[dict] = []
    for device in fleet.get("devices") or []:
        devices.append({"id": bounded(device.get("id", ""), 128), "vram_used": _u64(device.get("vram_used")),
                        "vram_total": _u64(device.get("vram_total"))})
    devices, devices_truncated = _truncated(devices, 24)
    kinds: List[dict] = []
    for kind in status.get("kinds") or []:
        kinds.append({"kind": bounded(kind.get("kind", ""), 128), "unit": bounded(kind.get("unit", ""), 128),
                      "scope": bounded(kind.get("scope", ""), 32)})
    kinds, kinds_truncated = _truncated(kinds, 24)
    degraded_all = [bounded(row, 120) for row in (fleet.get("degraded") or []) if isinstance(row, str)]
    degraded_all += [bounded(row, 120) for row in extra_degraded]
    degraded, degraded_truncated = _truncated(degraded_all, 4)
    demand: List[dict] = []
    for row in fleet.get("demand") or []:
        demand.append({"requires": bounded(row.get("requires", ""), 160), "waiting_s": _u64(row.get("waiting_s"))})
    demand, demand_truncated = _truncated(demand, 4)
    counts = [] if ring_counts is None else ring_counts
    body = {
        "node_state": bounded(status.get("node_state", ""), 32),
        "devices": devices, "devices_truncated": devices_truncated,
        "units": units, "units_truncated": units_truncated,
        "demand_unmet": _u64(fleet.get("demand_unmet")),
        "demand": demand, "demand_truncated": demand_truncated,
        "active_operations": [], "active_operations_truncated": 0,
        "leases_active": _u64(fleet.get("leases_active")),
        "completed": _ring(list(counts), start_s), "failed": _ring(list(counts), start_s),
        "evicted": _ring(list(counts), start_s),
        "degraded": degraded, "degraded_truncated": degraded_truncated,
        "membership_state": membership_state,
        "kinds": kinds, "kinds_truncated": kinds_truncated,
    }
    return body


def adapter_body(fleet: dict, status: dict) -> dict:
    """Exactly what the Stage 1 adapter produces from the same two documents (parity reference)."""
    membership_state = "fresh" if isinstance(status.get("membership"), dict) else "suspect"
    return _body(fleet, status, membership_state)


def _is_flat(fleet: dict) -> bool:
    return "hosts" not in fleet and any(k in fleet for k in ("units", "devices", "demand", "degraded"))


def _nodes_on(fleet: dict, host: str) -> List[dict]:
    hosts = fleet.get("hosts") or {}
    return [n for n in ((hosts.get(host) or {}).get("nodes") or []) if isinstance(n, dict)]


def normalise(fleet: dict, status: dict, now_s: float) -> Tuple[dict, dict, str]:
    """The flat (fleet, status) the body builder reads, plus the host id, from the real broker views."""
    host = bounded(fleet.get("vantage_host") or status.get("host_id") or "", 128)
    if _is_flat(fleet):
        return fleet, status, host
    nodes = _nodes_on(fleet, host)
    by_peer = {p.get("node_id") or p.get("device_id"): p for p in (status.get("peers") or []) if isinstance(p, dict)}
    units: List[dict] = []
    devices: Dict[str, dict] = {}
    kinds: List[dict] = []
    for node in nodes:
        for unit in node.get("units") or []:
            vram = (unit.get("footprint") or {}).get("vram_bytes")
            units.append({"name": unit.get("kind", ""), "kind": unit.get("kind", ""),
                          "residency": RESIDENCY.get(unit.get("residency"), "UNPINNED"),
                          "resident": unit.get("resident") is True, "busy": unit.get("busy") is True,
                          "vram_bytes": vram})
        mem = node.get("device_mem") or {}
        device_id = node.get("device_id")
        cap = (mem.get("capacity") or {}).get("vram_bytes") if isinstance(mem, dict) else None
        free = (mem.get("free") or {}).get("vram_bytes") if isinstance(mem, dict) else None
        if device_id and isinstance(cap, int) and device_id not in devices:
            devices[device_id] = {"id": device_id, "vram_total": cap,
                                  "vram_used": max(0, cap - free) if isinstance(free, int) else 0}
        scope = node.get("scope")
        for kind in node.get("kinds") or []:
            kinds.append({"kind": kind, "unit": kind, "scope": scope if isinstance(scope, str) else ""})
    if not units:  # a node that only appears in /status still has units worth reporting
        for peer in by_peer.values():
            for unit in peer.get("units") or []:
                vram = (unit.get("footprint") or {}).get("vram_bytes")
                units.append({"name": unit.get("kind", ""), "kind": unit.get("kind", ""),
                              "residency": RESIDENCY.get(unit.get("residency"), "UNPINNED"),
                              "resident": unit.get("resident") is True, "busy": unit.get("busy") is True,
                              "vram_bytes": vram})
    demand_doc = fleet.get("demand") or {}
    entries = demand_doc.get("entries") if isinstance(demand_doc, dict) else demand_doc
    demand = [{"requires": bounded(e.get("kind", "") + ("" if not e.get("selector") else
                                                         " " + json.dumps(e["selector"], sort_keys=True)), 160),
               "waiting_s": max(0, int(now_s - e["first_seen"])) if isinstance(e.get("first_seen"), (int, float)) else 0}
              for e in (entries or []) if isinstance(e, dict)]
    usage = ((fleet.get("quota") or {}).get("usage") or {}) if isinstance(fleet.get("quota"), dict) else {}
    states = [n.get("state") for n in nodes]
    membership = "fresh" if "fresh" in states else ("suspect" if "suspect" in states else "mia")
    flat = {"units": units, "devices": list(devices.values()), "degraded": list(fleet.get("degraded") or []),
            "demand": demand, "demand_unmet": len(demand), "leases_active": sum(v for v in usage.values()
                                                                               if isinstance(v, int))}
    node_state = "degraded" if (flat["degraded"] or membership != "fresh") else "up"
    return flat, {"kinds": kinds, "node_state": node_state, "membership_state": membership}, host


def fleet_body(fleet: dict, status: dict, now_s: float) -> dict:
    """The `harmony.fleet/1` body for this broker's host from the real `/fleet` and `/status` documents."""
    flat, flat_status, host = normalise(fleet, status, now_s)
    state = flat_status.get("membership_state")
    if state is None:
        state = "fresh" if isinstance(status.get("membership"), (dict, list)) and status.get("membership") else "suspect"
    start = int(now_s) - int(now_s) % RING_WIDTH_S
    body = _body(flat, flat_status, state, ring_counts=[0], start_s=start, extra_degraded=["counters_unavailable"])
    if host:
        body["host_id"] = host
    return body


# --- harmony.jobs/1 -----------------------------------------------------------------------------

JOB_STATES = ("queued", "placed", "running", "succeeded", "failed", "cancelled", "expired")


def _requires(spec: dict) -> str:
    selector = spec.get("selector") or {}
    text = ",".join(f"{k}={v}" for k, v in sorted(selector.items())) if isinstance(selector, dict) else ""
    return bounded(text or spec.get("handler", ""), 160)


def job_body(job: dict, now_ms: int) -> dict:
    """The `harmony.jobs/1` body for one workload-authority job (the shape `WorkloadStore._job` returns)."""
    from .workload_authority import classify_job  # local import keeps module import order trivial
    spec = job.get("spec") or {}
    raw_state = job.get("state", "queued")
    attempts = job.get("attempts") or []
    live = next((a for a in reversed(attempts) if a.get("state") == "running"), None)
    state = raw_state
    if raw_state == "running":
        state = "running" if job.get("progress") is not None else "placed"
    if state not in JOB_STATES:
        state = "failed" if raw_state not in ("queued",) else "queued"
    body: Dict[str, Any] = {"owner": bounded(job.get("owner", ""), 128), "handler": bounded(spec.get("handler", ""), 128),
                            "requires": _requires(spec), "state": state,
                            "queued_ms": int(float(job.get("created", 0)) * 1000)}
    if live is not None:
        body["attempt"] = {"id": bounded(live.get("id", ""), 128), "worker": bounded(live.get("worker", ""), 128),
                           "lease_until_ms": int(float(live.get("expires", 0)) * 1000),
                           "started_ms": int(float(job.get("updated", 0)) * 1000)}
    if raw_state in ("succeeded", "failed", "cancelled", "expired"):
        body["finished_ms"] = int(float(job.get("updated", 0)) * 1000)
        verdict = classify_job(job)
        if raw_state != "succeeded":
            kind = {"workload": "product", "infra": "infrastructure"}.get(verdict["attribution"], "unknown")
            if raw_state == "cancelled":
                kind = "refused" if "withdrawn" in verdict["reason"] else "unknown"
            body["outcome"] = {"kind": kind, "reason": bounded(verdict["reason"], 240)}
    return body
