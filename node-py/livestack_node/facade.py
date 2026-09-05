"""REST facade — the uniform /livestack surface over a polycore manager + a
LivestackCoordinator. ``gpu_call`` is supplied by the server: it runs a thunk
under that server's GPU discipline (polyasr's _transcribe_lock, polytts's single
_gpu_executor) and returns its result, so warm/evict never race in-flight work.
``fastapi`` is an optional dependency.
"""
from __future__ import annotations

import hashlib
import os
import time

from typing import Callable, Optional

from .lease import Capability


def resolve_device_id(host_id: str, explicit: Optional[str] = None) -> str:
    """The id of the device this node actually occupies.

    It used to be `f"{host_id}/gpu0"` — a string template, correct only on a
    single-GPU host. Two real costs on xc-tower-ubuntu (2x RTX 3090), 2026-09-05:
    a second polyasr had to be given a FAKE `host_id` to get its own device id,
    and a stale one later let the planner co-model a 5 GB ASR engine and a 20 GB
    LLM onto one card and evict the LLM. Device identity is a fact the node can
    read; it should not be guessed by its name.

    Resolution order — explicit argument, then `LIVESTACK_DEVICE_ID`, then
    derived from the backend:

    * CUDA — `{host_id}/{8 hex of the device UUID}`. The UUID is the driver's
      own identity for the physical card, so two processes pinned to the same
      card agree and two on different cards differ, with no configuration.
    * MLX — `{host_id}/mlx0`. Apple unified memory is one device.
    * neither — `{host_id}/gpu0`, today's value, so a single-GPU node that
      passes nothing sees no change at all.

    Never raises: identity must not be the thing that stops a node serving.
    """
    if explicit:
        return explicit
    env = (os.environ.get("LIVESTACK_DEVICE_ID") or "").strip()
    if env:
        return env
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(torch.cuda.current_device())
            uuid = getattr(props, "uuid", None)
            if uuid is not None:
                short = hashlib.sha256(str(uuid).encode()).hexdigest()[:8]
                return f"{host_id}/{short}"
            # A torch too old to expose `uuid` still knows which index it is on,
            # which is better than pretending every process is on gpu0.
            return f"{host_id}/gpu{torch.cuda.current_device()}"
    except Exception:
        pass
    try:
        import mlx.core  # noqa: F401
        return f"{host_id}/mlx0"
    except Exception:
        pass
    return f"{host_id}/gpu0"


def _load_report(coordinator, status, device_meter):
    """How busy this node is right now, for a consumer deciding where to send
    work. Computed at READ TIME from live state — a cached or periodically
    refreshed number would report an engine idle while it is saturated, which is
    worse than reporting nothing.

    Returns None when nothing can actually be measured. That distinction is the
    contract: a consumer must read an absent report as "no opinion" and fall
    back to its own latency ranking, NEVER as "idle". An engine that has gone
    quiet is the most likely source of an empty report, and reading silence as
    spare capacity steers traffic at exactly the node least able to serve it.

    `in_flight` counts real leases only. The coordinator issues `__usage__:`
    leases to keep an idle-evict clock alive; those mark recency, not work, and
    counting them would make a node that served one request ten minutes ago look
    permanently busy.
    """
    report = {}

    leases = status.get("active_leases") or []
    in_flight = sum(1 for l in leases
                    if not str(l.get("owner_id", "")).startswith("__usage__"))
    report["in_flight"] = in_flight
    report["resident_units"] = len(status.get("resident", []) or [])

    if device_meter is not None:
        try:
            mem = device_meter() or {}
            # meters.py returns {"capacity": {"vram_bytes": N}, "free": {...}} —
            # the resource-map shape the planner consumes, NOT flat ints. Read it
            # as written rather than assuming; the first cut of this function
            # assumed flat ints, and int() on a dict raised straight into the
            # except below, so a working CUDA meter reported no pressure at all
            # and nothing said why.
            cap = int((mem.get("capacity") or {}).get("vram_bytes") or 0)
            free = int((mem.get("free") or {}).get("vram_bytes") or 0)
            if cap > 0:
                report["device"] = {"capacity": cap, "free": free}
                # Fraction of the device in use, measured at the driver, so it
                # counts every process on the card and not just ours.
                report["pressure"] = round(max(0.0, min(1.0, 1.0 - free / cap)), 4)
        except Exception:
            # A meter that throws contributes nothing. It must not fabricate a
            # zero-pressure reading, which would advertise spare capacity we
            # just failed to establish.
            pass

    report["measured_at"] = time.time()
    return report


def build_router(manager, coordinator, capability: Capability,
                 gpu_call: Callable[[Callable], object],
                 device_meter: Optional[Callable[[], Optional[dict]]] = None,
                 activation_tracker=None,
                 readiness: Optional[Callable[[], Optional[dict]]] = None,
                 device_id: Optional[str] = None):
    # Resolved ONCE, here, so /capability and /residence can never disagree
    # about which device this node is on — a disagreement the broker would read
    # as two devices.
    device_id = resolve_device_id(capability.host_id, device_id)
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
            "device_id": device_id,
            "labels": dict(capability.labels),
            "units": list(manager.units.keys()),
            "resident": resident,
            "ready": bool(resident),
            "detail": "resident" if resident else "no unit resident",
        }
        load = _load_report(coordinator, st, device_meter)
        if load is not None:
            out["load"] = load
        if readiness is not None:
            try:
                supplied = readiness() or {}
                # The server's answer wins on fitness; it cannot invent units.
                for k in ("ready", "detail", "model", "concurrency", "region"):
                    if k in supplied:
                        out[k] = supplied[k]
                # A server that counts its own work reports better load than we
                # can infer from leases (polyasr knows its concurrent streams).
                # Merge rather than replace: the server supplies what it knows.
                if isinstance(supplied.get("load"), dict):
                    out["load"] = {**(out.get("load") or {}), **supplied["load"]}
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
               "device_id": device_id,
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
