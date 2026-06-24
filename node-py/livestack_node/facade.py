"""REST facade (port 2) — the uniform, language-neutral HTTP surface.

``build_router`` returns a FastAPI ``APIRouter`` a model server mounts into its
existing app. ``fastapi`` is an optional dependency: importing the rest of
``livestack_node`` never requires it.
"""

from __future__ import annotations

from typing import Optional

from .lease import Capability
from .residence import ResidenceController


def build_router(controller: ResidenceController, capability: Capability):
    try:
        from fastapi import APIRouter, Body, HTTPException
    except ImportError as exc:  # pragma: no cover - exercised only without fastapi
        raise RuntimeError(
            "livestack_node.facade requires fastapi; install 'livestack-node[facade]'"
        ) from exc

    router = APIRouter()

    @router.get("/capability")
    def get_capability() -> dict:
        return {
            "kind": capability.kind,
            "host_id": capability.host_id,
            "slots": capability.slots,
            "labels": dict(capability.labels),
            "lease_ttl_seconds": capability.lease_ttl_seconds,
            "units": [c.kind for c in controller._store.list_capabilities()],
        }

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok", "residence": controller.status()}

    @router.post("/lease")
    def acquire(payload: dict = Body(...)) -> dict:
        kind = payload.get("kind")
        if not kind:
            raise HTTPException(status_code=400, detail="'kind' is required")
        lease = controller.acquire(
            kind=kind,
            owner_id=payload.get("owner_id", "anonymous"),
            ttl_seconds=payload.get("ttl_seconds"),
        )
        if lease is None:
            raise HTTPException(status_code=409, detail=f"no capacity for kind '{kind}'")
        return {"lease_id": lease.lease_id, "kind": lease.capability_kind, "expires_at": lease.expires_at}

    @router.post("/lease/{lease_id}/heartbeat")
    def heartbeat(lease_id: str, payload: Optional[dict] = Body(None)) -> dict:
        ttl = (payload or {}).get("ttl_seconds")
        lease = controller.heartbeat(lease_id, ttl_seconds=ttl)
        if lease is None:
            raise HTTPException(status_code=404, detail=f"unknown lease '{lease_id}'")
        return {"lease_id": lease.lease_id, "expires_at": lease.expires_at}

    @router.post("/lease/{lease_id}/release")
    def release(lease_id: str) -> dict:
        return {"released": controller.release(lease_id)}

    @router.post("/model/warm")
    def warm(payload: dict = Body(...)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        controller._runtime.warm(unit)
        return {"resident": controller._runtime.resident()}

    @router.post("/model/evict")
    def evict(payload: dict = Body(...)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        if unit in controller._pinned:
            raise HTTPException(status_code=409, detail=f"unit '{unit}' is pinned")
        controller._runtime.evict(unit)
        return {"resident": controller._runtime.resident()}

    @router.post("/model/pin")
    def pin(payload: dict = Body(...)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        controller.pin(unit, pinned=bool(payload.get("pinned", True)))
        return {"pinned": sorted(controller._pinned)}

    return router
