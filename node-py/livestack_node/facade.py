"""REST facade — the uniform /livestack surface over a polycore manager + a
LivestackCoordinator. ``gpu_call`` is supplied by the server: it runs a thunk
under that server's GPU discipline (polyasr's _transcribe_lock, polytts's single
_gpu_executor) and returns its result, so warm/evict never race in-flight work.
``fastapi`` is an optional dependency.
"""
from __future__ import annotations

from typing import Callable, Optional

from .lease import Capability


def build_router(manager, coordinator, capability: Capability,
                 gpu_call: Callable[[Callable], object],
                 device_meter: Optional[Callable[[], Optional[dict]]] = None):
    try:
        from fastapi import APIRouter, Body, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("livestack_node.facade requires fastapi") from exc

    router = APIRouter()

    @router.get("/capability")
    def get_capability() -> dict:
        return {
            "kind": capability.kind,
            "host_id": capability.host_id,
            "labels": dict(capability.labels),
            "units": list(manager.units.keys()),
        }

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok", "residence": coordinator.status()}

    @router.post("/lease")
    def acquire(payload: dict = Body(...)) -> dict:
        kind = payload.get("kind")
        if not kind:
            raise HTTPException(status_code=400, detail="'kind' is required")
        lease = coordinator.acquire_lease(kind, payload.get("owner_id", "anonymous"),
                                          payload.get("ttl_seconds"))
        if lease is None:
            raise HTTPException(status_code=409, detail=f"no capacity for '{kind}'")
        gpu_call(lambda: manager.ensure(kind))  # warm on the GPU thread
        return {"lease_id": lease.lease_id, "kind": lease.capability_kind,
                "expires_at": lease.expires_at}

    @router.post("/lease/{lease_id}/heartbeat")
    def heartbeat(lease_id: str, payload: Optional[dict] = Body(None)) -> dict:
        lease = coordinator.heartbeat_lease(lease_id, (payload or {}).get("ttl_seconds"))
        if lease is None:
            raise HTTPException(status_code=404, detail=f"unknown lease '{lease_id}'")
        return {"lease_id": lease.lease_id, "expires_at": lease.expires_at}

    @router.post("/lease/{lease_id}/release")
    def release(lease_id: str) -> dict:
        return {"released": coordinator.release_lease(lease_id)}

    @router.post("/model/warm")
    def warm(payload: dict = Body(...)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        gpu_call(lambda: manager.ensure(unit))
        return {"resident": sorted(manager.resident)}

    @router.post("/model/evict")
    def evict(payload: dict = Body(...)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        if coordinator._pinned(unit):
            raise HTTPException(status_code=409, detail=f"unit '{unit}' is pinned")
        gpu_call(lambda: manager.request_evict(unit))
        return {"resident": sorted(manager.resident)}

    @router.get("/residence")
    def residence() -> dict:
        """Planner-facing view: every unit's footprint, residency tier, and whether
        it is resident / busy (an explicit, non-usage lease in flight). Lets a
        HostBroker build a planner WorldState from any node uniformly."""
        st = coordinator.status()
        busy = {l["kind"] for l in st.get("active_leases", [])
                if not str(l.get("owner_id", "")).startswith("__usage__")}
        resident = set(st.get("resident", []))
        units = []
        for kind, unit in manager.units.items():
            fp = getattr(unit, "footprint", 0) or 0
            units.append({
                "kind": kind,
                "footprint": {"vram_bytes": int(fp)},
                "residency": int(getattr(unit, "residency_policy", 2)),
                "resident": kind in resident,
                "busy": kind in busy,
            })
        out = {"host_id": capability.host_id,
               "device_id": f"{capability.host_id}/gpu0",
               "units": units}
        # Live measured device memory (capacity + real free), when a meter is wired.
        # Lets the Harmony planner reconcile against reality, not just footprints.
        if device_meter is not None:
            try:
                mem = device_meter()
                if mem:
                    out["device_mem"] = mem
            except Exception:
                pass
        return out

    return router
