"""hostd.py — the Harmony broker: a minimal host-broker HTTP daemon.

Harmony is Livestack's GPU-residency arbitration layer — priority-preemptive,
lease-based — that lets live (ASR/TTS), interactive (chipgen), and batch
(meeting-digest) workloads share one host's GPU in harmony instead of fighting.

Wraps a HostBroker + RestPeers for the model-server nodes sharing one host's GPU
and exposes the planner over HTTP so any consumer can ask for admission before it
loads a heavy unit:

    POST /admit  {"kind": "align"}   -> {"granted": true, "device_id": "...", "plan": "..."}
    GET  /status                      -> per-node residence snapshot
    GET  /plan                        -> dry-run desired plan (no dispatch)

Run:  python -m livestack_node.hostd
Config via env:
    LIVESTACK_PEERS      comma-separated /livestack base URLs
                         (default: polyasr 8766, polytts 8100, chipgen 8844 on localhost)
    LIVESTACK_HOST_ID    default zz-tower0
    LIVESTACK_VRAM_GB    device capacity (default 24)
    LIVESTACK_RESERVED_GB activation/driver slack the planner never allocates (default 2)
    LIVESTACK_BROKER_PORT default 8799
"""
from __future__ import annotations

import os
import time
from typing import List

from .hostbroker import HostBroker, RestPeer
from .planner import Device, Request, Evict, Grant, Load, plan as _plan

GB = 1_000_000_000

# Priority policy (lower = more important). The ASR pipeline (asr/align/diarize)
# outranks TTS, which outranks chipgen — so a digest'''s align can preempt idle
# TTS/chipgen. Decoupled from the residency tier on purpose: align/diarize are
# demand-driven (UNPINNED residence) yet high priority.
DEFAULT_PRIORITIES = {"asr": 10, "align": 15, "diarize": 15,
                      "qwen": 20, "voxcpm": 20, "chipgen": 30}

# Measured 2026-06-28 on zz-tower0 (real per-process VRAM occupancy in bytes,
# incl. the cached activation workspace torch holds after a forward pass — which
# is what actually fills the card). diarize runs on CPU (~50 MiB GPU) -> 0.5 GB floor.
DEFAULT_FOOTPRINTS = {"asr": 5_070_913_536, "align": 5_295_308_800,
                      "diarize": 524_288_000, "voxcpm": 6_543_114_240,
                      "qwen": 9_393_143_808, "chipgen": 5_259_657_216}


def build_broker(peer_urls: List[str], device_config=None,
                 default_vram_gb: float = 24.0, default_reserved_gb: float = 2.0) -> HostBroker:
    """Federated by default: devices are DISCOVERED from the peers (one per reported
    device_id, across however many hosts), sized from device_config[device_id] or the
    default. Point peer_urls at nodes on several hosts and the same broker plans and
    dispatches across all their GPUs."""
    peers = [RestPeer(u, priorities=DEFAULT_PRIORITIES, footprints=DEFAULT_FOOTPRINTS)
             for u in peer_urls]
    return HostBroker(devices=None, peers=peers, device_config=device_config or {},
                      default_capacity={"vram_bytes": int(default_vram_gb * GB),
                                        "reserved": int(default_reserved_gb * GB)},
                      clock=time.monotonic, log=lambda m: print(m, flush=True))


def build_app(broker: HostBroker):
    from fastapi import FastAPI, Body, HTTPException
    app = FastAPI(title="Livestack Harmony broker")
    state = {"last_evicted_at": {}}

    def _track(p):
        for ev in p.of(Evict):
            state["last_evicted_at"][ev.kind] = time.monotonic()

    @app.post("/admit")
    def admit(payload: dict = Body(...)):
        kind = payload.get("kind")
        if not kind:
            raise HTTPException(400, "'kind' required")
        req = Request(id=payload.get("id", f"{kind}-{int(time.monotonic() * 1000)}"),
                      kind=kind, owner=payload.get("owner", "consumer"),
                      created_at=time.monotonic())
        try:
            p = broker.plan_and_apply([req], state["last_evicted_at"])
        except Exception as e:  # a peer down etc. — degrade: let the caller proceed
            return {"granted": True, "device_id": None, "degraded": str(e)}
        _track(p)
        dev = next((g.device_id for g in p.of(Grant) if g.request_id == req.id), None)
        return {"granted": dev is not None, "device_id": dev, "plan": p.summary()}

    @app.get("/status")
    def status():
        out = []
        for peer in broker.peers:
            try:
                out.append(peer.refresh())
            except Exception as e:
                out.append({"error": str(e)})
        return {"peers": out, "last_evicted_at": state["last_evicted_at"]}

    @app.get("/plan")
    def plan_preview():
        world = broker.snapshot([], state["last_evicted_at"])
        return {"plan": _plan(world, broker.policy).summary(),
                "resident": [(p.kind, p.device_id) for p in world.placements]}

    # Proactive reconcile loop: re-snapshot + re-plan with NO pending request every
    # `interval` seconds, so Harmony reacts to the live situation BETWEEN admissions
    # — re-asserts the HARD_PIN floor, restores debounced SOFT_PINs once pressure
    # settles, and (since the snapshot now carries measured free) sheds idle units
    # when real VRAM drops below budget. Daemon thread: stops with the process.
    interval = float(os.environ.get("LIVESTACK_REPLAN_INTERVAL", "5"))
    if interval > 0:
        import threading

        def _reconcile_loop():
            while True:
                time.sleep(interval)
                try:
                    p = broker.plan_and_apply([], state["last_evicted_at"])
                    _track(p)
                    if p.of(Evict) or p.of(Load):
                        print(f"[harmony] reconcile: {p.summary()}", flush=True)
                except Exception as e:  # a peer down etc. — keep looping
                    print(f"[harmony] reconcile error: {e}", flush=True)

        threading.Thread(target=_reconcile_loop, name="harmony-reconcile",
                         daemon=True).start()

    return app


def main():
    peers_env = os.environ.get("LIVESTACK_PEERS", "").strip()
    if peers_env:
        peer_urls = [u.strip() for u in peers_env.split(",") if u.strip()]
    else:
        peer_urls = ["http://127.0.0.1:8766/livestack",   # polyasr
                     "http://127.0.0.1:8100/livestack",   # polytts
                     "http://127.0.0.1:8844/livestack"]   # chipgen
    import json
    device_config = {}
    dev_env = os.environ.get("LIVESTACK_DEVICES", "").strip()
    if dev_env:
        # {"host-b/gpu0": {"vram_gb": 48, "reserved_gb": 3}, ...}
        for did, c in json.loads(dev_env).items():
            device_config[did] = {"vram_bytes": int(float(c["vram_gb"]) * GB),
                                  "reserved": int(float(c.get("reserved_gb", 2)) * GB)}
    broker = build_broker(
        peer_urls, device_config=device_config,
        default_vram_gb=float(os.environ.get("LIVESTACK_VRAM_GB", "24")),
        default_reserved_gb=float(os.environ.get("LIVESTACK_RESERVED_GB", "2")),
    )
    import uvicorn
    port = int(os.environ.get("LIVESTACK_BROKER_PORT", "8799"))
    print(f"[harmony] broker on :{port} over {len(broker.peers)} peers", flush=True)
    uvicorn.run(build_app(broker), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
