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

from typing import Callable, Mapping, Optional

from .announce import node_region as _node_region
from .lease import Capability


def _machine_name(fallback: str) -> str:
    """The MACHINE a device belongs to — the hostname, not a node's chosen name.

    `LIVESTACK_MACHINE_ID` overrides it for hosts whose hostname is unstable
    (containers). Never raises: identity must not stop a node serving.
    """
    env = (os.environ.get("LIVESTACK_MACHINE_ID") or "").strip()
    if env:
        return env
    try:
        import socket
        name = socket.gethostname().strip()
        if name:
            return name.split(".")[0]
    except Exception:
        pass
    return fallback


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

    * CUDA — `{machine}/{8 hex of the device UUID}`. The UUID is the driver's
      own identity for the physical card, so two processes pinned to the same
      card agree and two on different cards differ, with no configuration.
    * MLX — `{machine}/mlx0`. Apple unified memory is one device.
    * neither — `{machine}/gpu0`.

    `machine` is the HOSTNAME, not the caller's `host_id`. That distinction is
    the whole correctness of this function and it was wrong: `host_id` is a name
    a node picks for itself, so three processes sharing one RTX 3090 announced
    it as three devices —

        xc-tower-ubuntu/4bac2869        polytts, polyasr
        xc-tower-ubuntu-b/4bac2869      polyasr #2
        xc-tower-ubuntu-gpu0/4bac2869   the LLM node

    — same card, three ids. The planner then split one card's free memory three
    ways, and dispatch could not match a grant to a peer: it placed a unit on
    `xc-tower-ubuntu/4bac2869` while the only node serving that unit called
    itself `...-gpu0/4bac2869`, so the grant succeeded and nothing ever loaded.
    The hostname distinguishes machines, the UUID distinguishes cards, and a
    node's chosen name distinguishes neither.

    Never raises: identity must not be the thing that stops a node serving.
    """
    if explicit:
        return explicit
    env = (os.environ.get("LIVESTACK_DEVICE_ID") or "").strip()
    if env:
        return env
    machine = _machine_name(host_id)
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(torch.cuda.current_device())
            uuid = getattr(props, "uuid", None)
            if uuid is not None:
                short = hashlib.sha256(str(uuid).encode()).hexdigest()[:8]
                return f"{machine}/{short}"
            # A torch too old to expose `uuid` still knows which index it is on,
            # which is better than pretending every process is on gpu0.
            return f"{machine}/gpu{torch.cuda.current_device()}"
    except Exception:
        pass
    try:
        import mlx.core  # noqa: F401
        return f"{machine}/mlx0"
    except Exception:
        pass
    return f"{host_id}/gpu0"


def resolve_device_candidates(host_id: str, explicit: Optional[str] = None) -> list:
    """Every device this node could load a unit on, primary first.

    `resolve_device_id` answers "where am I" — a question that only has one
    answer because a node is normally pinned to a card by CUDA_VISIBLE_DEVICES.
    That pin decides placement in a service config, outside the planner, and a
    decision the planner never sees is one it cannot revise when a card fills
    or frees. It is also what forces one service PROCESS per model per card.

    A node that can see several cards advertises them all and lets Harmony
    choose which one each unit lands on; alternating demand for two units then
    settles them one per card and keeps both warm, with no configuration saying
    so. A node that IS pinned — explicitly, or by LIVESTACK_DEVICE_ID — reports
    exactly one candidate and behaves as it always has.

    Never raises, never empty: the first entry is always `resolve_device_id`.
    """
    primary = resolve_device_id(host_id, explicit)
    if explicit or (os.environ.get("LIVESTACK_DEVICE_ID") or "").strip():
        return [primary]                  # the operator named it; do not second-guess
    out = [primary]
    try:
        import torch
        if not torch.cuda.is_available():
            return out
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            uuid = getattr(props, "uuid", None)
            did = (f"{host_id}/{hashlib.sha256(str(uuid).encode()).hexdigest()[:8]}"
                   if uuid is not None else f"{host_id}/gpu{i}")
            if did not in out:
                out.append(did)
    except Exception:
        pass                              # identity must never stop a node serving
    return out


def _load_report(coordinator, status, device_meter, in_flight_fn=None):
    """How busy this node is right now, for a consumer deciding where to send
    work. Computed at READ TIME from live state — a cached or periodically
    refreshed number would report an engine idle while it is saturated, which is
    worse than reporting nothing.

    Returns None when nothing can actually be measured. That distinction is the
    contract: a consumer must read an absent report as "no opinion" and fall
    back to its own latency ranking, NEVER as "idle". An engine that has gone
    quiet is the most likely source of an empty report, and reading silence as
    spare capacity steers traffic at exactly the node least able to serve it.

    `in_flight` is the server's own count when it supplies one (`in_flight_fn`,
    from `attach(in_flight=)`), and otherwise the count of real leases. The
    coordinator issues `__usage__:` leases to keep an idle-evict clock alive;
    those mark recency, not work, and counting them would make a node that
    served one request ten minutes ago look permanently busy.

    `in_flight_source` says WHICH, and it is the load-bearing field. A node that
    does not take a lease per request — polyasr streams, harmony-llm proxies —
    reports 0 leases while saturated, and a consumer cannot tell that from an
    idle node without being told where the number came from. `"server"` means
    the engine counted its own work; `"leases"` means we inferred it, and the
    consumer should weigh it accordingly rather than reading 0 as spare capacity.
    """
    report = {}

    if in_flight_fn is not None:
        try:
            report["in_flight"] = max(0, int(in_flight_fn()))
            report["in_flight_source"] = "server"
        except Exception:
            # A counter that throws contributes nothing — do NOT fall back to
            # the lease count under the "server" label, which would be a
            # confident wrong answer about how busy this engine is.
            in_flight_fn = None
    if in_flight_fn is None:
        leases = status.get("active_leases") or []
        report["in_flight"] = sum(
            1 for l in leases
            if not str(l.get("owner_id", "")).startswith("__usage__"))
        report["in_flight_source"] = "leases"
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
                # Unified memory is the SAME bytes as the host's RAM. Dropping
                # the flag here would leave every consumer of `load.device` to
                # add a Mac's 30 GB device to its 36 GB host and report a
                # machine with 66 GB.
                if mem.get("unified"):
                    report["device"]["unified"] = True
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
                 device_id: Optional[str] = None,
                 in_flight: Optional[Callable[[], int]] = None,
                 node_id: Optional[str] = None, inventory=None,
                 node_principals=None,
                 subsystems: Optional[Mapping[str, Callable[[], Mapping]]] = None):
    # Resolved ONCE, here, so /capability and /residence can never disagree
    # about which device this node is on — a disagreement the broker would read
    # as two devices.
    #
    # ``subsystems`` are the named health probes /health merges in beside
    # ``residence`` — name -> zero-arg callable -> a small dict carrying at
    # least ``state``. Healthy states (``ok``/``absent``/``attached``) leave
    # the overall status alone; anything else degrades it, named. A probe that
    # THROWS degrades too, with the error named — a crashing health probe is
    # itself a degradation signal (jidoka).
    device_id = resolve_device_id(capability.host_id, device_id)
    device_candidates = resolve_device_candidates(capability.host_id, device_id
                                                  if device_id != resolve_device_id(capability.host_id)
                                                  else None)
    try:
        from fastapi import APIRouter, Body, Depends, Header, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("livestack_node.facade requires fastapi") from exc

    # WHO may pull the node's levers. Same shape and same rules as the fleet
    # token table (one file, mode 0600, refused if world-readable) — see
    # fleet_auth.principals_from_env. Unset means every endpoint keeps today's
    # open behaviour, which is the right default for a single-operator fleet
    # and exactly what an unconfigured deployment must see. attach() may pass
    # a pre-computed table so the app's audit middleware resolves principals
    # against the SAME one; standalone callers (tests) get the env default.
    if node_principals is None:
        from .fleet_auth import principals_from_env
        node_principals = principals_from_env(
            file_var="LIVESTACK_NODE_TOKENS_FILE",
            inline_var="LIVESTACK_NODE_TOKENS",
            log=lambda m: print(m, flush=True))
    if node_principals:
        print(f"[livestack] /lease and /model/* require a bearer token; "
              f"{len(node_principals)} principal(s): "
              + ", ".join(sorted(p.name for p in node_principals.values())),
              flush=True)

    def _require_node_principal(authorization: str = Header(None)):
        """Gate for the endpoints that change what is resident. A warm, an
        evict, a reclaim and a lease all move GPU bytes or hold capacity, so
        they carry the same requirement as the broker's writes: a valid node
        credential, 401 without one. The read endpoints (/residence,
        /capability, /health) deliberately stay open — a consumer must be able
        to discover a node it cannot yet authenticate to. ``None`` (no source
        configured) keeps today's open behaviour; an empty table (source
        configured but refused or malformed) FAILS CLOSED — 401 for everyone,
        an alarm state, never silent."""
        if node_principals is None:
            return None
        from .fleet_auth import AuthError, bearer_token, principal_for
        try:
            return principal_for(node_principals, bearer_token(authorization))
        except AuthError as e:
            raise HTTPException(status_code=e.status, detail=e.detail)

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
            "node_id": node_id,
            # Where this node is, from its own environment.
            #
            # Reported HERE as well as in the announce because the announce
            # only reaches brokers this node was told about, and a fleet broker
            # on another host learns remote nodes by SEEDING plus probing —
            # never by announce. Measured: after regions were announced,
            # xc-tower-ubuntu's own nodes showed `na` and every remote one
            # (xc-mac-studio, zz-tower0) still showed `None`, because a seed
            # carries no region and nothing else on that path did either.
            "region": _node_region(),
            "device_id": device_id,
            "device_candidates": device_candidates,
            "labels": dict(capability.labels),
            "units": list(manager.units.keys()),
            "resident": resident,
            "ready": bool(resident),
            "detail": "resident" if resident else "no unit resident",
        }
        # What this node HAS, as opposed to what it IS: the voice ids a TTS
        # server holds, the models an ASR has on disk. Evaluated per request
        # rather than snapshotted at attach, because the answer changes while
        # the process runs — cloning a voice adds one — and a stale inventory
        # sends work to a node that no longer matches. Never fatal: a node
        # that cannot list its inventory still serves its kind.
        if inventory is not None:
            try:
                have = inventory() if callable(inventory) else inventory
                if isinstance(have, dict) and have:
                    out["inventory"] = {str(k): v for k, v in have.items()}
            except Exception as e:
                out["inventory_error"] = str(e)[:200]
        load = _load_report(coordinator, st, device_meter, in_flight)
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
                    merged = {**(out.get("load") or {}), **supplied["load"]}
                    # A readiness-supplied in_flight is still the SERVER's count,
                    # so the provenance must follow it. Otherwise a node that
                    # reports its own streams through the legacy path is labelled
                    # "leases" and a consumer discounts a number it should trust.
                    if "in_flight" in supplied["load"]:
                        merged["in_flight_source"] = "server"
                    out["load"] = merged
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
        out = {"status": "ok", "residence": coordinator.status()}
        for name, probe in (subsystems or {}).items():
            try:
                snap = dict(probe() or {})
            except Exception as e:  # noqa: BLE001 - a crashing probe IS a signal
                snap = {"state": "error", "error": f"health probe failed: {e}"}
            out[name] = snap
            # Healthy states keep "ok"; anything else degrades the surface by
            # name. HTTP stays 200 — the bytes carry the truth, and the broker
            # self-probe gates on /residence, not on this status word.
            if snap.get("state") not in (None, "ok", "absent", "attached"):
                out["status"] = "degraded"
        return out

    @router.post("/lease")
    def acquire(payload: dict = Body(...),
                _principal=Depends(_require_node_principal)) -> dict:
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
    def warm(payload: dict = Body(...),
             _principal=Depends(_require_node_principal)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        # `device` is the planner's placement. Units whose loader does not take
        # one ignore it, so a pinned single-device node is unaffected.
        device = payload.get("device") or payload.get("device_id")
        if device and device not in device_candidates:
            raise HTTPException(
                status_code=409,
                detail=f"device '{device}' is not one this node can load on "
                       f"({', '.join(device_candidates)})")
        budget = payload.get("budget") or None
        gpu_call(lambda: manager.ensure(unit, device=device, budget=budget))
        return {"resident": sorted(manager.resident), "device": device or device_id}

    @router.post("/model/evict")
    def evict(payload: dict = Body(...),
              _principal=Depends(_require_node_principal)) -> dict:
        unit = payload.get("unit")
        if not unit:
            raise HTTPException(status_code=400, detail="'unit' is required")
        if coordinator._pinned(unit):
            raise HTTPException(status_code=409, detail=f"unit '{unit}' is pinned")
        gpu_call(lambda: manager.request_evict(unit))
        return {"resident": sorted(manager.resident)}

    @router.post("/model/reclaim")
    def reclaim(payload: dict = Body(default={}),
                _principal=Depends(_require_node_principal)) -> dict:
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
            grp = getattr(manager.units.get(kind), "spread_group", "")
            if grp:
                entry["spread_group"] = grp
            attrs = getattr(manager.units.get(kind), "attributes", None)
            if attrs:
                entry["attributes"] = dict(attrs)
            # Unit economics the operator declared (the harmony-llm unit
            # file): emitted ONLY when set, so an undeclared unit's residence
            # report is byte-for-byte what a node that predates the fields
            # produces — and the broker keeps its defaults for it.
            eco = manager.units.get(kind)
            if getattr(eco, "min_residency_s", None) is not None:
                entry["min_residency_s"] = float(eco.min_residency_s)
            if getattr(eco, "reload_cost", None) is not None:
                entry["reload_cost"] = float(eco.reload_cost)
            # An explicit priority from the node outranks the broker's
            # tier-derived default (_RES_TO_PRIO): the node measured what the
            # unit costs, the tier only guesses.
            if getattr(eco, "priority", None) is not None:
                entry["priority"] = int(eco.priority)
            units.append(entry)
        out = {"host_id": capability.host_id,
               # WHICH PROCESS this is. `host_id` is a name a node picks (two
               # polyasr on one card deliberately differ), and `device_id` is
               # the card they share — neither says "this is the same server you
               # already have, reached by another URL", which is what a broker
               # holding both a localhost seed and an announced address needs.
               "node_id": node_id,
               "device_id": device_id,
               # Where this node COULD place a unit, not just where it is. The
               # planner needs the choice to have a choice.
               "device_candidates": device_candidates,
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
        # System RAM, and this process's share of it. A node is not only what it
        # holds on a card: an ASR server's buffers, an LLM's page tables and a
        # CPU-only node's entire working set live here, and a reader with only
        # `device_mem` sees a machine as empty while it swaps.
        try:
            from .meters import host_mem
            hm = host_mem()
            if hm:
                out["host_mem"] = hm
        except Exception:
            pass
        return out

    return router
