"""attach() — the one call both polyasr and polytts make, identically, to become
livestack nodes. The server builds its ManagedUnits (with per-unit
ResidencyPolicy for pinning) and supplies a ``gpu_call`` that runs a thunk under
its GPU discipline; everything else is uniform.

    manager, coord = attach(app, host_id="zz-tower0", kind="polyasr",
                            units=units, idle_seconds=180, coload=True,
                            gpu_call=gpu_call)

Only ``coload`` (True for polyasr's co-resident units, False for polytts'
one-model-in-VRAM engines) and which unit is HARD_PIN differ between the two.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable, Dict, Optional

from .coordinator import LivestackCoordinator
from .facade import _machine_name, build_router, resolve_device_id
from .lease import Capability


def _footprint_signature(units: Dict[str, object]) -> str:
    """A short fingerprint of the unit set + declared footprints. The persisted
    activation store is keyed by this, so a model/footprint change invalidates stale
    values instead of trusting a possibly-too-low reserve (the OOM direction)."""
    items = sorted((str(n), int(getattr(u, "footprint", 0) or 0)) for n, u in units.items())
    return hashlib.sha256(repr(items).encode()).hexdigest()[:16]


class WorkCounter:
    """The server's own count of the work it is doing, for ``attach(in_flight=)``.

    Both a counter and a context manager, on purpose: one object is passed to
    ``attach`` and wrapped around the handler, so the number reported and the
    number maintained cannot drift apart::

        busy = counting()
        attach(app, ..., in_flight=busy)

        @app.post("/transcribe")
        async def transcribe(...):
            async with busy:
                return await do_work()

    Thread-safe, and correct under an exception (the decrement is in a
    ``finally``): a handler that raises must not leave the engine looking
    permanently busy, which is the failure mode that would strand a healthy node.
    """

    __slots__ = ("_n", "_lock")

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        return self._n

    def __int__(self) -> int:
        return self._n

    def acquire(self) -> "WorkCounter":
        """Count one unit of work. Pair with :meth:`release`.

        The explicit pair exists for work whose lifetime does not fit a ``with``
        block — a streaming response, say, which is in flight until its last byte
        reaches the caller and so outlives the handler that created it.
        """
        with self._lock:
            self._n += 1
        return self

    def release(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    def __enter__(self) -> "WorkCounter":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()

    async def __aenter__(self) -> "WorkCounter":
        return self.__enter__()

    async def __aexit__(self, *_exc) -> None:
        self.__exit__()


def counting() -> WorkCounter:
    """A fresh :class:`WorkCounter`. See ``attach(in_flight=)``."""
    return WorkCounter()


def attach(app, *, host_id: str, kind: str, units: Dict[str, object],
           idle_seconds: int, coload: bool, gpu_call: Callable[[Callable], object],
           prefix: str = "/livestack", device_meter="auto", port=None,
           readiness: Callable[[], dict] = None,
           device_id: Optional[str] = None,
           in_flight: Optional[Callable[[], int]] = None,
           inventory=None,
           preload=None):
    """``device_meter``: a zero-arg callable -> measured {capacity,free} (see
    meters.py), ``"auto"`` to pick one by backend (CUDA/MLX), or ``None`` to report
    no live memory. Defaulting to "auto" means a node becomes memory-aware on
    redeploy with no server-side change.

    ``device_id`` names the device this node occupies. Omit it and it is derived
    from the backend (CUDA device UUID / MLX / the legacy ``{host_id}/gpu0``) —
    see :func:`livestack_node.facade.resolve_device_id`. A node that passes
    nothing on a single-GPU host keeps exactly today's id.

    ``in_flight`` is the server's own count of the work it is currently doing.
    Supply it whenever a request does not take a lease — a streaming ASR socket,
    an LLM proxy — because the lease-derived fallback reports 0 for those while
    the engine is saturated. The report always says which of the two it is
    (``load.in_flight_source``: ``"server"`` or ``"leases"``), so a consumer can
    tell "0 because idle" from "0 because this node cannot see its own work".
    :func:`livestack_node.counting` is the usual way to maintain it.

    ``inventory`` is what this node HAS, as opposed to what it IS: a mapping
    like ``{"voice": [...]}``, or a zero-arg callable returning one. It is
    published on ``/capability`` and reaches the fleet broker, so a caller can
    ask for "a polytts in North America that has THIS voice" in one request
    (``/fleet/rank?require=voice:<id>``) instead of resolving a host and then
    discovering the voice lives on the other one. Pass a callable when the
    answer changes at runtime — cloning a voice adds one.

    ``preload`` warms a unit (a name, a list of names, or a zero-arg callable)
    AFTER the server is answering, not before. Use it instead of loading a
    model in a startup hook — see :func:`_start_preload` for the deadlock that
    costs.

    Each ``manager.run()`` GPU op is bracketed by an :class:`ActivationObserver` that
    measures that unit's exact peak activation and reports it as headroom for the planner
    to reserve. Learned high-waters persist across restarts (keyed by a footprint
    signature, so a model change discards stale values). Disable with
    ``LIVESTACK_ACT_SAMPLE_S=0``; absent CUDA (MLX/CPU) the node just doesn't
    live-measure."""
    from .manager import ModelManager
    from .measure import ActivationTracker, ActivationObserver, alloc_meter

    # Resolve identity BEFORE the meter, because the meter is checked against it:
    # a meter reading a card this process is not on reports a confident wrong
    # number, which is worse than reporting none (see meters.auto_meter).
    device_id = resolve_device_id(host_id, device_id)

    if device_meter == "auto":
        from .meters import auto_meter
        # The meter is checked AGAINST the resolved identity: a meter reading a
        # card this process is not on produces a confident wrong number, which
        # is worse than producing none.
        device_meter = auto_meter(device_id)

    tracker = None
    observer = None
    if os.environ.get("LIVESTACK_ACT_SAMPLE_S", "1") != "0":
        meter = alloc_meter()
        if meter is not None:
            store = os.environ.get("LIVESTACK_ACT_STORE")
            if store is None:
                d = os.path.expanduser("~/.cache/livestack")
                try:
                    os.makedirs(d, exist_ok=True)
                    store = os.path.join(d, f"activation-{host_id}-{kind}.json")
                except Exception:
                    store = None
            tracker = ActivationTracker(store_path=store or None,
                                        signature=_footprint_signature(units))
            observer = ActivationObserver(tracker, meter=meter)

    coordinator = LivestackCoordinator(host_id, coload=coload, usage_ttl_seconds=idle_seconds)
    manager = ModelManager(units, idle_seconds, coordinator=coordinator,
                           activation_observer=observer)

    # WHICH PROCESS this is, as opposed to which card it is on or what it calls
    # itself. A broker routinely holds the same node twice — once as a localhost
    # seed and once as the address the node announced — and counted it as two,
    # so one polytts became two resident voxcpm and the planner modelled 35 GB of
    # units on a 24 GB card. Hostname plus listening port is the one pair that
    # says "same process" across two URLs; without a port there is no identity to
    # state, and the broker keeps its old (duplicating) behaviour rather than
    # guessing one.
    early_port = port if port is not None else os.environ.get("LIVESTACK_NODE_PORT")
    node_id = f"{_machine_name(host_id)}:{int(early_port)}" if early_port else None

    app.include_router(
        build_router(manager, coordinator, Capability(kind=kind, host_id=host_id),
                     gpu_call, device_meter=device_meter, activation_tracker=tracker,
                     readiness=readiness, device_id=device_id,
                     in_flight=in_flight, node_id=node_id, inventory=inventory),
        prefix=prefix,
    )

    # Report for duty. This is what makes starting a model server the ONLY
    # action required to be arbitrated — no LIVESTACK_PEERS edit, no broker
    # restart. The port is the one fact the broker cannot infer (a POST shows
    # it the source address, not the listening port), and the caller already
    # has it because it is about to pass the same number to uvicorn.
    #
    # No port ⇒ no registration, silently-but-once. A node embedded in someone
    # else's server is still perfectly usable as a seeded peer; refusing to
    # start over it would be worse than not announcing.
    resolved_port = early_port
    if os.environ.get("LIVESTACK_REGISTER", "1") != "0" and resolved_port:
        from .announce import start_registrar
        # The HOST a node announces itself at. Loopback is right for the host
        # broker on the same machine and WRONG for a fleet broker on another —
        # it would register a peer it can never reach, and the peer would sit
        # `suspect` forever with a connect error, looking like a dead node
        # rather than a misconfigured address.
        #
        # A node cannot infer this: it does not reliably know its own mesh
        # address, and the URL is the one fact the broker cannot learn from the
        # POST (which shows it a source address, not a listening port). So it is
        # the operator's to state, and the default keeps single-machine
        # deployments working with nothing set.
        advertise = (os.environ.get("LIVESTACK_NODE_HOST") or "127.0.0.1").strip()
        from .announce import node_region
        start_registrar(
            f"http://{advertise}:{int(resolved_port)}{prefix}",
            host_id=host_id, kind=kind,
            # Where this machine is. Announced, not inferred: see
            # `announce.node_region` for why a measured distance cannot answer
            # it and why a caller must treat unknown as excluded.
            region=node_region(),
            interval_s=float(os.environ.get("LIVESTACK_REGISTER_INTERVAL", "30")),
        )

    if preload is not None:
        _start_preload(preload, manager=manager, gpu_call=gpu_call,
                       facade_url=(f"http://127.0.0.1:{int(resolved_port)}{prefix}"
                                   if resolved_port else None))

    return manager, coordinator


def _start_preload(preload, *, manager, gpu_call, facade_url: Optional[str],
                   answers: Optional[Callable[[str], bool]] = None,
                   log: Callable[[str], None] = print,
                   sleep: Callable[[float], None] = time.sleep,
                   attempts: int = 30, interval_s: float = 2.0):
    """Warm a node's units once it is reachable, on a thread of its own.

    **The deadlock this exists to break.** A node that loads its model inside
    its web framework's startup hook does not bind its port until the load
    returns. It therefore cannot be snapshotted, so the broker never learns
    which kinds it hosts — and `admit` for an unknown kind is a REFUSAL, by
    design, because a refusal and an outage must not look alike. Measured on
    xc-mac-studio, 2026-09-18: polytts called `manager.ensure("qwen")` from its
    startup hook, the local broker answered
    `defer polytts-708943429 (unknown kind)`, and the process sat with no
    listener, no sockets and no CPU. The fleet saw `mia`, connection refused,
    for forty-six hours. Nothing was broken except the order.

    Warming after the bind breaks the cycle at the only place it can be broken:
    the node becomes reachable, the registrar's announce is backed by a facade
    that answers, the broker snapshots it and learns its kinds, and the warm
    then asks for admission of something the planner has heard of.

    A node is `ready: false` in the meantime, which the fleet view already
    models and `fleet_rank` already filters on — so an unwarmed node is not
    chosen, rather than chosen and slow.
    """
    from .announce import facade_answers

    answers = answers or facade_answers

    def _warm_one(item) -> None:
        # A CALLABLE runs as it is, on this thread. A NAME is marshalled with
        # `gpu_call`, because the framework is the one deciding where that load
        # happens.
        #
        # The difference is not a style choice, it is a deadlock. A node's GPU
        # executor is normally a single worker — Metal thread affinity and MPS
        # both require it — and a caller's own warm thunk marshals to that same
        # executor. Wrapping it means `gpu_call(thunk)` occupies the one worker
        # and the thunk then submits to the same pool and waits for a worker
        # that will never come. Measured on xc-tower-ubuntu, 2026-09-18:
        # polytts warmed through `_gpu_executor.submit(...).result()`, the
        # preload thread hung on the first load, nothing ever became resident
        # and every /tts after it queued behind the wedged worker — a TTS node
        # that answered /health with 200 and synthesized nothing at all.
        if callable(item):
            item()
            return
        gpu_call(lambda: manager.ensure(item))

    items = ([preload] if callable(preload) or isinstance(preload, str)
             else list(preload))

    def _run() -> None:
        # Wait for our own front door. Bounded: a server that never binds has a
        # problem this thread cannot fix, and a warm thread spinning forever
        # against it would hide that.
        if facade_url:
            for _ in range(attempts):
                if answers(facade_url):
                    break
                sleep(interval_s)
            else:
                log(f"[livestack] preload gave up: {facade_url} never answered")
                return
        for item in items:
            name = getattr(item, "__name__", item)
            try:
                _warm_one(item)
                log(f"[livestack] preloaded {name}")
            except Exception as e:  # noqa: BLE001 - a failed warm must not kill the node
                # Deliberately not fatal. A node that cannot warm is a node
                # that reports `ready: false` and is not chosen; a node that
                # exits takes its whole surface with it, including the
                # endpoints that would have said why.
                log(f"[livestack] preload of {name} failed: {e}")

    t = threading.Thread(target=_run, name="livestack-preload", daemon=True)
    t.start()
    return t
