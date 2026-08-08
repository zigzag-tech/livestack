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
    LIVESTACK_DEVICES    hosted devices, e.g. {"buildhost-a": {"hosted": true,
                         "concurrency": 1, "labels": {"arch": "linux/amd64"}}}
    LIVESTACK_UNITS      kinds no peer reports, e.g. {"build": {"priority": 20}}
    LIVESTACK_PROBES     health probes for hosted devices:
                         {"buildhost-a": {"cmd": "docker info", "interval_s": 60}}
    LIVESTACK_LEASE_TTL_S hosted-lease expiry without heartbeats (default 120)
"""
from __future__ import annotations

import os
import time
from typing import Dict, List

from .hostbroker import HostBroker, RestPeer
from .membership import MembershipPolicy, RosterFull
from .planner import Device, Request, Residency, Unit, Evict, Grant, Load, plan as _plan

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
                 default_vram_gb: float = 24.0, default_reserved_gb: float = 2.0,
                 membership=None, extra_units=None) -> HostBroker:
    """Federated by default: devices are DISCOVERED from the peers (one per reported
    device_id, across however many hosts), sized from device_config[device_id] or the
    default. Point peer_urls at nodes on several hosts and the same broker plans and
    dispatches across all their GPUs. extra_units declares kinds no peer will ever
    report (the peerless case: a BUILD host whose "build" unit lives only in config)."""
    peers = [RestPeer(u, priorities=DEFAULT_PRIORITIES, footprints=DEFAULT_FOOTPRINTS)
             for u in peer_urls]
    return HostBroker(devices=None, peers=peers, device_config=device_config or {},
                      default_capacity={"vram_bytes": int(default_vram_gb * GB),
                                        "reserved": int(default_reserved_gb * GB)},
                      clock=time.monotonic, log=lambda m: print(m, flush=True),
                      membership=membership, extra_units=extra_units)


def build_app(broker: HostBroker):
    from fastapi import FastAPI, Body, HTTPException
    app = FastAPI(title="Livestack Harmony broker")
    state = {"last_evicted_at": {}}
    # Hosted-backend health probes (LIVESTACK_PROBES), run on the reconcile
    # loop's cadence. probe_state is what /status reports under "hosted".
    import json as _json
    probes = _json.loads(os.environ.get("LIVESTACK_PROBES", "").strip() or "{}")
    probe_state: Dict[str, dict] = {}

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
                      created_at=time.monotonic(),
                      selector=payload.get("selector") or {})
        try:
            p = broker.plan_and_apply([req], state["last_evicted_at"])
        except Exception as e:  # a peer down etc. — degrade: let the caller proceed
            return {"granted": True, "device_id": None, "degraded": str(e)}
        _track(p)
        dev = next((g.device_id for g in p.of(Grant) if g.request_id == req.id), None)
        lease_id = None
        if dev is not None and broker.device_config.get(dev, {}).get("hosted"):
            # A hosted grant is only half-done until the ledger knows: without a
            # lease the next admit would double-book the same concurrency slot.
            # A checkout failure must NOT void the grant — the caller got its
            # device; the ledger is bookkeeping, and snapshot expiry heals it.
            try:
                lease_id = broker.hosted_checkout(dev, kind, req.owner)
            except Exception as e:
                print(f"[harmony] hosted checkout failed dev={dev}: {e}", flush=True)
        return {"granted": dev is not None, "device_id": dev, "plan": p.summary(),
                "lease_id": lease_id}

    @app.post("/lease/{lease_id}/heartbeat")
    def lease_heartbeat(lease_id: str):
        """Proof of life from a hosted leaseholder. Unknown/expired is a False,
        not an error — the answer the client needs is 'do I still hold the slot'."""
        return {"ok": broker.hosted_heartbeat(lease_id)}

    @app.post("/lease/{lease_id}/release")
    def lease_release(lease_id: str):
        return {"ok": broker.hosted_release(lease_id)}

    @app.post("/devices/{device_id}/health")
    def device_health(device_id: str, payload: dict = Body(...)):
        """A hosted backend reporting its own health (or an operator forcing it).
        The gate is a plain boolean; 'detail' is for humans reading /status."""
        available = bool(payload.get("available", True))
        broker.set_hosted_available(device_id, available)
        probe_state[device_id] = {"cmd": None, "ok": available,
                                  "tail": payload.get("detail", ""),
                                  "at": time.monotonic(), "source": "reported"}
        return {"ok": True}

    @app.post("/peers")
    def register(payload: dict = Body(...)):
        """A node reporting for duty.

        This is the endpoint that makes starting a model server the only action
        required: no LIVESTACK_PEERS edit, no broker restart. Idempotent on
        facade_url, so a node restart re-registers and a broker restart refills
        from the nodes within one heartbeat — the same soft-state property the
        broker already claims for placements, extended to membership.
        """
        url = (payload.get("facade_url") or "").strip()
        if not url:
            raise HTTPException(400, "'facade_url' required")
        try:
            return broker.register_url(
                url,
                make_peer=lambda u: RestPeer(u, priorities=DEFAULT_PRIORITIES,
                                             footprints=DEFAULT_FOOTPRINTS),
                host_id=payload.get("host_id"),
                device_id=payload.get("device_id"),
                kinds=payload.get("kinds"),
                readiness=payload.get("readiness"),
            )
        except RosterFull as e:
            raise HTTPException(429, str(e))

    @app.get("/peers")
    def peers():
        """Membership with per-peer state and how long each has been unseen —
        so 'is that node gone, or did it blip?' is answerable without grepping
        a log that used to print the same line every 5 seconds."""
        return {"peers": broker.membership_snapshot()}

    @app.get("/status")
    def status():
        out = []
        for peer in broker.peers:
            try:
                out.append(peer.refresh())
            except Exception as e:
                out.append({"error": str(e)})
        # Hosted backends have no peer to report them, so their health and the
        # prober's view surface here — otherwise a gated-off build host is
        # invisible exactly when you need to see why.
        hosted = {}
        for did, cfg in broker.device_config.items():
            if not cfg.get("hosted"):
                continue
            hosted[did] = {"available": broker.hosted_available.get(did, True),
                           "probe": probe_state.get(did)}
        return {"peers": out, "membership": broker.membership_snapshot(),
                "last_evicted_at": state["last_evicted_at"], "hosted": hosted}

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

        def _run_probes(now):
            """Run each due hosted-health probe; exit 0 means the backend is a
            candidate again, anything else gates it out. One probe wedging must
            never take the reconcile loop with it — hence the per-probe try."""
            import subprocess
            for did, cfg in probes.items():
                st = probe_state.get(did)
                if st and now - st.get("at", 0.0) < float(cfg.get("interval_s", 60)):
                    continue
                try:
                    r = subprocess.run(cfg["cmd"], shell=True, timeout=20,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    ok = r.returncode == 0
                    tail = (r.stdout or b"").decode(errors="replace")[-300:]
                except Exception as e:
                    ok, tail = False, str(e)
                probe_state[did] = {"cmd": cfg["cmd"], "ok": ok, "tail": tail,
                                    "at": now, "source": "probe"}
                broker.set_hosted_available(did, ok)

        def _reconcile_loop():
            while True:
                time.sleep(interval)
                try:
                    # Reclaim BEFORE planning. A node that has evicted everything
                    # and still holds its allocator pool is memory no unit claims,
                    # so the planner has nothing to evict and would shed innocent
                    # units to relieve pressure it cannot actually fix. Give the
                    # pool back first, then plan against what is really free.
                    broker.sweep_leaks()
                    # Announce membership changes even when nothing probed this
                    # tick — a roster that rots in silence is the failure being
                    # fixed, so the sweep must not depend on probes happening.
                    broker.roster.tick()
                    # Forget registered peers gone past the prune window. Seeds
                    # survive, and an unset window prunes nothing at all.
                    broker.prune_absent()
                    # Probe BEFORE planning, so this cycle's snapshot plans
                    # against health that is seconds old, not minutes.
                    _run_probes(time.monotonic())
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
    # Nodes report for duty (POST /peers), so these localhost guesses are no
    # longer how membership is *meant* to work — but they stay, as seeds.
    #
    # Deleting them was the first instinct and it was wrong. The objection to a
    # guess was that a wrong seed and a dead node produced identical output
    # (this fleet's polyasr serves 8765, not the 8766 named here) — and that is
    # precisely what membership now fixes: a wrong seed shows up in GET /peers
    # as `mia` with its connect error, probed on a backoff instead of every
    # cycle. The guess became cheap and visible at the same moment it became
    # unnecessary.
    #
    # What removing them would have cost is real: every deployment that relies
    # on this default loses its whole roster on upgrade, silently, until its
    # nodes are also upgraded to announce themselves. zz-tower0 is exactly that
    # deployment — no LIVESTACK_PEERS in its unit file, three peers found only
    # by this default.
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
        # Local:  {"host-b/gpu0": {"vram_gb": 48, "reserved_gb": 3}, ...}
        # Hosted: {"qwen-sg": {"hosted": true, "concurrency": 8,
        #                      "cost_bias": -1, "labels": {"region": "apac-sg"}}}
        #
        # A NEGATIVE cost_bias makes the hosted backend the default — it beats
        # even a warm local replica, which is how interactive ASR moves off the
        # card and leaves it for align/diarize/chipgen. A positive bias makes it
        # overflow-only. Hosted rows carry no vram and are never discovered.
        for did, c in json.loads(dev_env).items():
            if c.get("hosted"):
                device_config[did] = {k: c[k] for k in
                                      ("hosted", "concurrency", "cost_bias", "labels", "host_id")
                                      if k in c}
                continue
            device_config[did] = {"vram_bytes": int(float(c["vram_gb"]) * GB),
                                  "reserved": int(float(c.get("reserved_gb", 2)) * GB)}
    # Kinds no peer reports — e.g. "build" on a peerless BUILD-host broker:
    # {"build": {"priority": 20}}. Without this, admit() defers "unknown kind".
    extra_units = {}
    units_env = os.environ.get("LIVESTACK_UNITS", "").strip()
    if units_env:
        for kind, u in json.loads(units_env).items():
            extra_units[kind] = Unit(kind, {}, priority=u.get("priority", 100),
                                     residency=Residency.UNPINNED)
    # Membership thresholds. The defaults are the whole point — a fleet should
    # not have to configure these to get sane behaviour. LIVESTACK_PEER_PRUNE
    # is deliberately UNSET by default: this bound deletes rather than rotates,
    # and an unset window must mean disabled, never "delete on the next deploy".
    prune_env = os.environ.get("LIVESTACK_PEER_PRUNE_SECONDS", "").strip()
    membership = MembershipPolicy(
        suspect_after_s=float(os.environ.get("LIVESTACK_PEER_SUSPECT_SECONDS", "45")),
        mia_after_s=float(os.environ.get("LIVESTACK_PEER_MIA_SECONDS", "600")),
        prune_after_s=float(prune_env) if prune_env else None,
        max_peers=int(os.environ.get("LIVESTACK_MAX_PEERS", "32")),
    )
    broker = build_broker(
        peer_urls, device_config=device_config,
        default_vram_gb=float(os.environ.get("LIVESTACK_VRAM_GB", "24")),
        default_reserved_gb=float(os.environ.get("LIVESTACK_RESERVED_GB", "2")),
        membership=membership,
        extra_units=extra_units,
    )
    import uvicorn
    port = int(os.environ.get("LIVESTACK_BROKER_PORT", "8799"))
    print(f"[harmony] broker on :{port} over {len(broker.peers)} seeded peers "
          f"(nodes self-register at POST /peers; GET /peers shows membership)",
          flush=True)
    uvicorn.run(build_app(broker), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
