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
                 device_meter: Optional[Callable[[], Optional[dict]]] = None,
                 activation_tracker=None,
                 readiness: Optional[Callable[[], Optional[dict]]] = None):
    try:
        from fastapi import APIRouter, Body, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("livestack_node.facade requires fastapi") from exc

    router = APIRouter()

    @router.get("/capability")
    def get_capability() -> dict:
        """The node's readiness descriptor — a stable, documented statement of
        what this node is and whether it is fit to serve.

        It exists so a consumer stops scraping `/health`, whose shape is each
        server's own business and drifts. A benchday daemon deciding whether to
        announce this engine as pool capacity reads THIS, and `ready` here means
        the model is loaded and the server's own functional probe passes — not
        that a port accepted a connection.

        `readiness` is supplied by the server because only it knows what fit
        means (polyasr has a streaming probe; polytts has a loaded voice set).
        Absent one, we report the generic truth we can actually stand behind:
        the process is serving and these units are resident.
        """
        st = coordinator.status()
        resident = list(st.get("resident", []))
        out = {
            "kind": capability.kind,
            "host_id": capability.host_id,
            "device_id": f"{capability.host_id}/gpu0",
            "labels": dict(capability.labels),
            "units": list(manager.units.keys()),
            "resident": resident,
            "ready": bool(resident),
            "detail": "resident" if resident else "no unit resident",
        }
        if readiness is not None:
            try:
                supplied = readiness() or {}
                # The server's answer wins on fitness; it cannot invent units.
                for k in ("ready", "detail", "model", "concurrency", "region"):
                    if k in supplied:
                        out[k] = supplied[k]
            except Exception as e:
                # A readiness probe that throws is NOT ready. Reporting the
                # generic fallback here would claim fitness we just failed to
                # establish, which is the direction that sends audio to a
                # broken engine.
                out["ready"] = False
                out["detail"] = f"readiness probe failed: {e}"
        return out

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

    def _process_mem() -> Optional[dict]:
        try:
            from .meters import cuda_self_meter
            return cuda_self_meter()()
        except Exception:
            return None

    def _run_free() -> None:
        """Every backend's reclaim, best-effort. `freeing` already knows CUDA vs
        MLX vs libc; this is the first thing that calls it from outside a
        model unload."""
        import gc as _gc
        from . import freeing
        _gc.collect()
        freeing.free_cuda()
        freeing.free_mlx()
        freeing.trim_ram()

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

    @router.post("/model/reclaim")
    def reclaim(payload: dict = Body(default={})) -> dict:
        """Hand the allocator's reserved-but-unused pool back to the driver.

        Eviction drops a model; it does NOT necessarily return that model's VRAM.
        PyTorch keeps freed blocks in a per-process cache, so a node can report
        every unit `resident: false` and still hold the card — which is exactly
        how a polytts node sat on 14.7 GB while polyasr beside it failed every
        request with `CUDA out of memory. Tried to allocate 2.00 MiB`.

        Detection alone could not fix that: Harmony's only lever is evicting
        units, and the memory belonged to no unit. This is the missing lever —
        the owning process is the only thing that can give the pool back.

        Runs on the GPU executor like every other device-touching call, so it
        cannot race a load or a generate. Reports before/after so the caller can
        see whether it actually recovered anything, rather than assuming.
        """
        before = _process_mem()
        gpu_call(_run_free)
        after = _process_mem()
        freed = 0
        if before and after:
            freed = max(0, int(before.get("reserved_bytes", 0)) - int(after.get("reserved_bytes", 0)))
        return {"freed_bytes": freed, "before": before, "after": after,
                "resident": sorted(manager.resident)}

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
            entry = {
                "kind": kind,
                "footprint": {"vram_bytes": int(fp)},
                "residency": int(getattr(unit, "residency_policy", 2)),
                "resident": kind in resident,
                "busy": kind in busy,
            }
            # Measured peak-activation headroom (allocator high-water minus declared
            # weights), when a tracker is wired. The planner reserves it on-device
            # while the unit is resident so runtime activation can't OOM.
            if activation_tracker is not None:
                hb = activation_tracker.headroom_bytes(kind)
                if hb > 0:
                    entry["activation_headroom"] = {"vram_bytes": int(hb)}
            units.append(entry)
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
        # What THIS process holds, and whether its resident units explain it.
        #
        # `device_mem` answers "how full is the card" — it cannot answer "who is
        # holding it". That gap caused an outage: a node reported every unit
        # `resident: false` while still holding 14.7 GB in the allocator's pool,
        # so the planner saw nothing to evict and a neighbouring ASR server died
        # of OOM. The condition is now stated wherever residence is read.
        try:
            from .meters import cuda_self_meter, leak_signal
            self_usage = cuda_self_meter()()
            if self_usage:
                out["process_mem"] = self_usage
                resident_fp = sum(int(getattr(manager.units[k], "footprint", 0) or 0)
                                  for k in resident if k in manager.units)
                leak = leak_signal(self_usage, resident_fp)
                if leak:
                    out["leak"] = leak
        except Exception:
            pass
        return out

    return router
