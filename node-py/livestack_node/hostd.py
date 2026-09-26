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
    GET  /fleet                       -> the whole-fleet view (see fleet_view())

The same process runs as a FLEET BROKER with ``LIVESTACK_DISPATCH=observe``:
peers on every host, plans over all of them, and dispatches nothing. That is the
safety property — one card, one master — and it is proven by absence: no
``[hostbroker] evict``/``warm`` line may appear in a fleet broker's journal while
the host brokers' own journals keep showing theirs. See
``_plans/fleet-broker.md``.

Run:  python -m livestack_node.hostd
Config via env:
    LIVESTACK_PEERS      comma-separated /livestack base URLs
                         (default: polyasr 8766, polytts 8100, chipgen 8844 on localhost;
                         "none" = explicitly peerless, e.g. the build broker)
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
    LIVESTACK_FLEET_TOKENS_FILE  mode-0600 JSON file of bearer token ->
                         principal, read FIRST (the file wins over the inline
                         env; a world-readable file is REFUSED — see
                         fleet_auth.principals_from_env). When principals
                         exist, POST /admit and POST /fleet/admit REQUIRE a
                         token and the owner comes from it, never the body:
                           {"<tok>": {"name":"media-corpus","owner":"media-corpus"},
                            "<tok>": {"name":"hub","delegate_prefix":"acct_"}}
                         `"delegate_prefix": "*"` is the ONE prefix that means
                         every owner: the engine case, where the caller asserts
                         the owner and the engine only relays it. An empty or
                         absent prefix is refused, so that principal cannot be
                         minted by a typo.
                         A fixed principal is one service with one identity; a
                         delegating one has authenticated somebody else and may
                         name an owner inside its prefix. Unset = no auth,
                         which makes any quota advisory — the startup line and
                         GET /fleet both say so. Quote the inline form for
                         systemd (bare double quotes are stripped).
    LIVESTACK_FLEET_TOKENS  inline fallback of the same JSON, used only when
                         the file variable is unset. Prefer the file: the one
                         secret in the system should not be inline JSON.
    LIVESTACK_NODE_CONTROL_TOKEN_FILE mode-0600 file containing the bearer token
                         hostd sends when it warms, evicts, or reclaims a node.
                         Reads remain unauthenticated. Required when nodes set
                         LIVESTACK_NODE_TOKENS_FILE.
    LIVESTACK_ACCOUNT_QUOTA  max concurrent fleet slots ONE account may hold.
                         UNSET = no ceiling, which is the right default for a
                         single-operator fleet and the WRONG one the day
                         strangers can register. A refused admit is a 429 naming
                         the count, never a silent demotion.
                         Worth only as much as `owner` is: it is a string the
                         caller supplies, so fronting public registration means
                         the owner must arrive from an authenticated channel.
    LIVESTACK_ACCOUNT_QUOTAS  per-account overrides, e.g. {"media-corpus": 8}
    LIVESTACK_FAIR_SHARE_PENALTY_S  seconds of urgency an account forfeits per
                         slot it already holds, applied to the deadline ordering
                         (default 30; 0 disables). A no-op on a single-tenant
                         fleet, where every job carries the same owner.
    LIVESTACK_DISPATCH   "apply" (default) = a host broker, warms and evicts its
                         own host's units. "observe" = a FLEET broker: same
                         planning, same /fleet view, dispatches NOTHING.
    LIVESTACK_BROKER_URL (read by NODES, not by the broker) comma-separated
                         brokers a node reports for duty to — its host broker and,
                         with a fleet broker deployed, that too. Announcing to
                         both is what lets LIVESTACK_PEERS shrink back to meaning
                         "the operator says this ought to exist".
    LIVESTACK_LINK_PEERS comma-separated OTHER host brokers' base URLs. Each is
                         timed on the reconcile loop and its own `links` row is
                         collected, so the fleet holds a measured MATRIX rather
                         than one broker's star. Unmeasured pairs have no opinion.
    LIVESTACK_RELAYS     JSON of relay vantage points the fleet cannot measure
                         itself: {"<id>": {"region": "na", "links": {"<host>":
                         <ms>}}}. A caller with no links row ranks from
                         `vantage=relay:<id>`; declared links are carried into
                         the fleet view as `relays`.
    LIVESTACK_CAPABILITY_TTL  seconds a node's /capability descriptor is cached
                         for the /fleet view (default 15). /fleet is a poll
                         surface and probes to Nanjing cost 0.5-1.5s each.
    LIVESTACK_LEDGER     "0" disables decision-ledger emission entirely
    LIVESTACK_LEDGER_DIR where ledgers are written (default ~/.cache/livestack)
    LIVESTACK_LEDGER_MAX_MB / _FILES / _AGE_DAYS   the bound. AGE_DAYS is unset
                         by default and unset means the age window is DISABLED —
                         a delete-shaped bound must never default to deleting.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .hostbroker import HostBroker, RestPeer
from .mesh_peer import MeshPeer, RelayRoute, facade_id
from . import relay_control
from .membership import MembershipPolicy, RosterFull
from .fleet_scheduler import SchedulerPolicy
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


def make_peer(url, *, priorities=None, fallback_footprints=None,
              control_token=None, relay_config=None, relay_account=None,
              relay_device=None) -> RestPeer:
    """Scheme-selecting Peer factory: the one place a roster URL is turned into
    a dialer. `http(s)://` → RestPeer (unchanged); `mesh://` → MeshPeer over
    the meshlink relay stack. Anything else is refused by name — an unmapped
    scheme must not fall through to urllib and fail there as "unknown url
    type", forty minutes later, in a different process."""
    if url.startswith(("http://", "https://")):
        return RestPeer(url, priorities=priorities,
                        fallback_footprints=fallback_footprints,
                        control_token=control_token)
    if url.startswith("mesh://"):
        import socket
        cfg = relay_config
        if cfg is None:
            cfg = relay_control.RelayConfig.from_env()
        relays = [RelayRoute(url=u, relay_id=rid) for u, rid in
                  _relay_ids_for(cfg.urls).items()]
        return MeshPeer(url, relays=relays, relay_config=cfg,
                        account_id=relay_account or "livestack-broker",
                        device_id=relay_device or socket.gethostname(),
                        priorities=priorities,
                        fallback_footprints=fallback_footprints,
                        control_token=control_token)
    raise ValueError(f"unsupported peer URL scheme in {url!r} "
                     "(expected http(s):// or mesh://)")


def _relay_ids_for(urls) -> Dict[str, str]:
    """Relay URL → relay_id for capability minting.

    The parser lives in relay_control (next to the minting both sides mint
    against); hostd keeps this name so its call sites and its historical
    unprefixed skip lines stay unchanged."""
    return relay_control.relay_ids_from_env(
        urls, log=lambda m: print(m, flush=True))


def build_broker(peer_urls: List[str], device_config=None,
                 default_vram_gb: float = 24.0, default_reserved_gb: float = 2.0,
                 membership=None, extra_units=None, dispatch: bool = True,
                 ledger=None, emitter_id: str = "host-broker",
                 node_control_token: Optional[str] = None) -> HostBroker:
    """Federated by default: devices are DISCOVERED from the peers (one per reported
    device_id, across however many hosts), sized from device_config[device_id] or the
    default. Point peer_urls at nodes on several hosts and the same broker plans and
    dispatches across all their GPUs. extra_units declares kinds no peer will ever
    report (the peerless case: a BUILD host whose "build" unit lives only in config).

    Peer URLs keep their scheme: http(s) facade URLs become RestPeers, mesh://
    URLs become MeshPeers — to planning and membership both remain opaque
    URL-keyed records."""
    peers = [make_peer(u, priorities=DEFAULT_PRIORITIES,
                       fallback_footprints=DEFAULT_FOOTPRINTS,
                       control_token=node_control_token)
             for u in peer_urls]
    return HostBroker(devices=None, peers=peers, device_config=device_config or {},
                      default_capacity={"vram_bytes": int(default_vram_gb * GB),
                                        "reserved": int(default_reserved_gb * GB)},
                      clock=time.monotonic, log=lambda m: print(m, flush=True),
                      membership=membership, extra_units=extra_units,
                      dispatch=dispatch, ledger=ledger,
                      emitter="host-broker" if dispatch else "fleet-broker",
                      emitter_id=emitter_id)


def build_app(broker: HostBroker):
    from fastapi import FastAPI, Body, Header, HTTPException
    from fastapi.responses import HTMLResponse

    from .ui import page as ui_page
    app = FastAPI(title="Livestack Harmony broker")
    # One journal line per mutating request and per auth refusal, with source
    # address and principal name — the evidence the R.3 inventory gate reads
    # ("zero 401s from an address not in the inventory"). Reads stay silent;
    # see request_log.py for the reasoning and what is never logged.
    from . import request_log
    request_log.attach(
        app,
        principal_for=lambda headers: request_log.principal_label(
            headers.get("authorization"),
            getattr(broker, "fleet_principals", None)))
    state = {"last_evicted_at": {}}
    # Hosted-backend health probes (LIVESTACK_PROBES), run on the reconcile
    # loop's cadence. probe_state is what /status reports under "hosted".
    import json as _json
    probes = _json.loads(os.environ.get("LIVESTACK_PROBES", "").strip() or "{}")
    probe_state: Dict[str, dict] = {}

    def _track(p):
        for ev in p.of(Evict):
            state["last_evicted_at"][ev.kind] = time.monotonic()

    def _read_principal(broker, authorization) -> Optional[str]:
        """The principal NAME a credential on a READ endpoint resolves to, or
        None. Reads stay open — no credential, unknown token, or auth not
        configured all mean "anonymous" and are recorded as such (null), never
        refused: the refusal machinery belongs to the write endpoints."""
        principals = getattr(broker, "fleet_principals", None)
        if not principals or not authorization:
            return None
        from .fleet_auth import AuthError, bearer_token, principal_for
        try:
            return principal_for(principals, bearer_token(authorization)).name
        except AuthError:
            return None

    @app.post("/admit")
    def admit(payload: dict = Body(...), authorization: str = Header(None)):
        kind = payload.get("kind") or ""
        requires = payload.get("requires") or {}
        if not kind and not requires:
            raise HTTPException(400, "'kind' or 'requires' is required")
        # WHO, before the request is planned — exactly as /fleet/admit: the
        # credential decides the owner; the body is consulted only for a
        # principal that was granted delegation, and only inside its prefix.
        # With no auth SOURCE configured (None) this is skipped entirely and
        # the owner comes from the body, byte-for-byte the unauthenticated
        # behaviour. An empty-but-configured table ({}: refused file,
        # malformed source) is the opposite state — auth ON with nobody
        # admitted, failing closed.
        principals = getattr(broker, "fleet_principals", None)
        if principals is not None:
            from .fleet_auth import AuthError, authenticate
            try:
                owner, principal = authenticate(
                    principals, authorization, payload.get("owner"))
            except AuthError as e:
                # 403 from a DELEGATING principal is ledgered under the
                # principal's name: a known caller tried to spend capacity
                # outside the prefix it was granted, and that attempt is a
                # fact a retrospective needs. A fixed principal naming a
                # mismatched owner is a caller confusing its own identity —
                # refused, and recorded against NEITHER owner, because nothing
                # was spent and attributing a refusal to an account it never
                # acted as would pollute that account's record. A 401 has no
                # principal to name, so it writes nothing.
                if e.status == 403:
                    from .fleet_auth import bearer_token, principal_for
                    try:
                        who = principal_for(principals,
                                            bearer_token(authorization))
                    except AuthError:
                        who = None
                    if who is not None and who.delegates:
                        broker.emit_admit(
                            {"kind": kind or None, "candidates": [],
                             "target": None, "reason": e.detail,
                             "refused": "auth_prefix"},
                            {"owner": payload.get("owner") or None,
                             "principal": who.name,
                             "refused": e.detail})
                raise HTTPException(e.status, e.detail)
        else:
            owner, principal = (payload.get("owner", "consumer"), None)
        req = Request(id=payload.get("id", f"{kind or 'req'}-{int(time.monotonic() * 1000)}"),
                      kind=kind, owner=owner,
                      created_at=time.monotonic(),
                      selector=payload.get("selector") or {},
                      requires=requires,
                      # Set only when a fronting engine vouched for `owner`
                      # via its inbound X-Harmony-Owner header; the Grant
                      # record carries it so the ledger can tell an asserted
                      # owner from the engine's own identity. Never trusted
                      # from an anonymous caller to LIFT an owner — it only
                      # annotates whichever owner the credential path resolved.
                      owner_asserted=bool(payload.get("owner_asserted")))
        # NOTE on the degrade branch below. It answers `granted: True` for ANY
        # exception, which tells the caller to proceed — and a caller that loads
        # a model on that word puts it on a card the planner never cleared.
        # Observed 2026-09-07: a KeyError inside planning came back as
        # permission, and a node ran vLLM into a card still holding a 22 GB
        # model. The narrow fix is below: a plan that places nothing now answers
        # `granted: False` with a reason, so a refusal and an outage stop
        # looking alike. Distinguishing FAULTS from refusals inside
        # `plan_and_apply` is the remaining half and wants its own change.
        try:
            p = broker.plan_and_apply([req], state["last_evicted_at"])
        except Exception as e:  # a peer down etc. — degrade: let the caller proceed
            return {"granted": True, "device_id": None, "degraded": str(e)}
        _track(p)
        grant = next((g for g in p.of(Grant) if g.request_id == req.id), None)
        dev = grant.device_id if grant is not None else None
        served_kind = grant.kind if grant is not None else None
        lease_id = None
        if dev is not None and broker.device_config.get(dev, {}).get("hosted"):
            # A hosted grant is only half-done until the ledger knows: without a
            # lease the next admit would double-book the same concurrency slot.
            # A checkout failure must NOT void the grant — the caller got its
            # device; the ledger is bookkeeping, and snapshot expiry heals it.
            try:
                lease_id = broker.hosted_checkout(dev, served_kind or kind, req.owner)
            except Exception as e:
                print(f"[harmony] hosted checkout failed dev={dev}: {e}", flush=True)
        return {"granted": dev is not None, "device_id": dev, "plan": p.summary(),
                # WHICH unit satisfied it. A caller that asked for "an llm >= 20B"
                # has no other way to know what it got, and it needs the name to
                # address the completion.
                "kind": served_kind,
                # How much room the unit actually has, so the node can size the
                # engine to it instead of an operator guessing a fraction.
                "budget": dict(grant.budget) if grant is not None else {},
                "lease_id": lease_id,
                **({} if dev is not None else
                   {"reason": "the planner could not place it on any device"})}

    # `:path`, because a fleet lease id names its device and a hosted device
    # id is a URL (`http://100.64.0.18:8190-<ms>-<seq>`). A bare `{lease_id}`
    # cannot match once the server decodes the `%2F`s, so every release and
    # heartbeat for such a lease was a 404 and it lived out its TTL — enough
    # to hold a caller's whole quota between calls (attune, 2026-09-23).
    @app.post("/lease/{lease_id:path}/heartbeat")
    def lease_heartbeat(lease_id: str):
        """Proof of life from a hosted leaseholder. Unknown/expired is a False,
        not an error — the answer the client needs is 'do I still hold the slot'."""
        return {"ok": broker.hosted_heartbeat(lease_id)}

    @app.post("/lease/{lease_id:path}/release")
    def lease_release(lease_id: str, payload: Optional[dict] = Body(default=None)):
        """Hand the slot back. An optional body ``{"status": "ok"|"failed",
        "wall_s": <seconds>}`` reports how the job went; it becomes the
        lease's `caller_ok`/`job_wall_s` outcome (scheduler-policy-routine
        §1). A value that is not exactly that shape is REFUSED (422), never
        coerced: an outcome that was guessed is worse than none."""
        extra = _release_report(payload or {})
        return {"ok": broker.hosted_release(lease_id, **extra)}

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
                make_peer=lambda u: make_peer(
                    u, priorities=DEFAULT_PRIORITIES,
                    fallback_footprints=DEFAULT_FOOTPRINTS,
                    control_token=getattr(broker, "node_control_token", None)),
                host_id=payload.get("host_id"),
                device_id=payload.get("device_id"),
                region=payload.get("region"),
                # Who this node is pooled for, as the operator or the
                # enrolling hub stated it. Passed through uninterpreted:
                # the roster records the grant; the admission path enforces
                # it (`fleet_admit.targets_from_view`).
                scope=payload.get("scope"),
                # The provisioning operation that created this node, as the
                # node states it. Recorded uninterpreted; the operation store
                # is what joins it back to the create it paid for.
                operation_id=payload.get("operation_id"),
                kinds=payload.get("kinds"),
                readiness=payload.get("readiness"),
            )
        except RosterFull as e:
            raise HTTPException(429, str(e))

    @app.get("/peers")
    def peers():
        """Membership with per-peer state and how long each has been unseen —
        so 'is that node gone, or did it blip?' is answerable without grepping
        a log that used to print the same line every 5 seconds.

        Also the CHEAP endpoint, and that is load-bearing: it is what other
        brokers time to measure the link between hosts, and what they read this
        broker's own `links` row from. `/status` would refresh every node on this
        host per caller, so a collector polling it would multiply this host's
        probe cost by the number of collectors.
        """
        return {"host_id": broker.host_id,
                "peers": broker.membership_snapshot(),
                "links": {k: round(v, 1) for k, v in broker.link_ms.items()}}

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
                "last_evicted_at": state["last_evicted_at"], "hosted": hosted,
                "host_id": broker.host_id,
                "links": {k: round(v, 1) for k, v in broker.link_ms.items()}}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        """The fleet resource map, for a person rather than a consumer.

        Served by whichever broker is asked: a host broker draws the machine it
        arbitrates, the fleet broker draws every machine it can see. It is a
        VIEW — it polls `/fleet` and offers no button that warms, evicts or
        reclaims, because one card has one master (this one, if it dispatches)
        and a page that could preempt from a phone is a second one.
        """
        return HTMLResponse(ui_page())

    @app.get("/fleet")
    def fleet():
        """The whole-fleet view: every node the broker knows, grouped by host,
        with membership state, measured probe distance, readiness and load.

        An absence is a ROW, never a gap — a peer that cannot be read still
        appears with its state, its age and its last error. A view that omits
        what it cannot reach can only report health, which is not what anyone
        opens it to find out.
        """
        view = broker.fleet_view()
        # The elastic pools in force and the operations outstanding, beside the
        # machines that exist. A reader asking "why did nothing burst?" needs to
        # see that NO pool is configured, and one asking "what is this instance
        # doing on my bill?" needs to see the operation that created it —
        # neither is answerable from a list of nodes.
        pools = getattr(broker, "fleet_pools", ())
        view["pools"] = [
            {"id": p.id, "provider": p.provider, "tier": p.tier.name,
             "region": p.region, "instance_type": p.instance_type,
             "cost_per_hour": p.cost_per_hour, "max_instances": p.max_instances,
             "kinds": list(p.kinds),
             "adapter": p.provider in getattr(broker, "fleet_providers", {})}
            for p in pools]
        demand = getattr(broker, "fleet_demand", None)
        if demand is not None:
            # What the fleet was asked for and could not give, still inside its
            # TTL. Reported because "nothing bursts" and "nothing was asked for"
            # are opposite problems that look identical in a plan.
            view["demand"] = demand.snapshot()
        store = getattr(broker, "operation_store", None)
        if store is not None:
            active = store.active()
            view["operations"] = {
                "active": [op.to_dict() for op in active],
                # Reported rather than discovered. An operation whose ledger
                # write failed still applied, and the gap in the audit trail is
                # a fact an operator has to be able to see without grepping.
                "observability_degraded": [op.operation_id for op in active
                                           if op.observability_degraded],
                "store": store.path,
            }
        runtime = getattr(broker, "policy_runtime", None)
        if runtime is not None:
            # The target-choice policy's state, and what is wrong with it. This
            # broker has no subsystem-health mechanism, so the degradations are
            # listed here (scheduler-policy-routine design §7): running on
            # defaults, native/reference disagreement, a refused artifact, a
            # missing native module, a record stream dropping or unavailable.
            view["policy"] = runtime.status()
            view["degraded"] = list(view["policy"]["degraded"])
        return view

    @app.get("/fleet/rank")
    def fleet_rank(kind: str, vantage: str = "direct", via: str = None,
                   region: str = None, regions: str = None,
                   require: str = None,
                   prefer: str = None,
                   allow_unknown_region: bool = False, ttl_s: float = 60.0,
                   authorization: str = Header(None)):
        """Where should a `kind` request START, from this vantage.

        Advisory, and bounded: the response carries `generated_at` and `ttl_s`,
        and a consumer must DISCARD past the TTL rather than downgrade — a stale
        ranking is worse than none, because none falls back to a working default
        while stale looks authoritative. A wrong first guess costs one hop; the
        client picker still probes and fails over.

        `require=` is the same idea for capability: `require=voice:3240e99…`
        keeps only the nodes that ADVERTISE that voice. It is what lets a
        caller ask for "a polytts in North America that has this voice" in one
        request, instead of resolving a host and then discovering the voice is
        on the other one. A node that advertises nothing is not a match — a
        server that cannot say it has the voice cannot be sent the request.

        **The broker still does not DECIDE region; it will APPLY one it is
        handed.** Those are different things and the difference is the whole
        design. `region=` remains the asker's own region, recorded and never
        applied, so a ledger row can be read later. `regions=` is a policy the
        CALLER states in the request — "only these" — and the broker filters
        with it, reporting every exclusion and why.

        Filtering here rather than in each caller is not the broker taking the
        policy over: the answer is a function of the request, and it changes
        the moment the request does. What it buys is one implementation of the
        rule instead of one per language — the alternative is a TypeScript
        consumer reimplementing "which hosts are North American" as a literal
        list, which is exactly the second place to be wrong that this design
        exists to avoid.
        """
        from .fleet_rank import rank as _rank
        from .preferences import PreferenceError, parse_preferences
        try:
            preferences = parse_preferences(prefer)
        except PreferenceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = _rank(broker.fleet_view(), kind, vantage=via or vantage,
                       ttl_s=ttl_s, prefer=preferences)
        # `region` is RECORDED, never applied. It is the asker's region as the
        # emitter knew it, which is what makes a ledger record readable later —
        # but WHERE the work may run is `regions`, below.
        result["asker_region"] = region
        # WHO asked, when they said: a read stays open without a credential,
        # but a VALID one is recorded on the ledger row beside the owner, so a
        # retrospective can tell "the hub, looking" from "nobody, looking".
        # Reads never refuse over a bad token — that is what the write
        # endpoints are for.
        result["principal"] = _read_principal(broker, authorization)

        wanted = [r.strip().lower() for r in (regions or "").split(",") if r.strip()]
        if wanted:
            from .client import eligible_targets
            kept, rejected = eligible_targets(result, allow_regions=set(wanted),
                                              allow_unknown_region=allow_unknown_region)
            result["targets"] = kept
            result["region_policy"] = {"allow": wanted,
                                       "allow_unknown": bool(allow_unknown_region),
                                       "rejected": rejected}
            # The chosen row must agree with the filtered list, or a caller
            # reading `chosen` gets an answer the policy just refused.
            result["chosen"] = kept[0]["target_id"] if kept else None
            if not kept:
                result["reason"] = (
                    f"no {kind} target in {'/'.join(wanted)}: "
                    + "; ".join(f"{r['target_id']} ({r['why']})" for r in rejected[:4])
                ) or f"no {kind} target in {'/'.join(wanted)}"

        # Capability AFTER region, so the reason names the rule that actually
        # emptied the list. Run first, it emptied `targets` and the region
        # filter then overwrote its message with "no polytts target in na" —
        # true, and not why.
        from .client import capable_targets, parse_requirements
        requirements = parse_requirements(require)
        if requirements:
            kept, rejected = capable_targets(result, requirements)
            result["targets"] = kept
            result["capability_policy"] = {"require": requirements, "rejected": rejected}
            result["chosen"] = kept[0]["target_id"] if kept else None
            if not kept and rejected:
                need = ", ".join(f"{k}={v}" for k, v in requirements.items())
                result["reason"] = (
                    f"no {kind} target advertising {need}: "
                    + "; ".join(f"{r['target_id']} ({r['why']})" for r in rejected[:4]))

        broker.emit_rank(result)
        return {k: v for k, v in result.items() if k != "candidates"}

    @app.post("/fleet/admit")
    def fleet_admit(payload: dict = Body(...), authorization: str = Header(None)):
        """Throw a task at the fleet: "where should this run?"

        The answer names a HOST and a NODE, and stops there. Whether the unit is
        resident on that node, and whether loading it would evict something,
        stays with that host's own broker — the caller's request to the node
        triggers its `manager.ensure` -> its host broker's `/admit`, exactly as
        today. Two brains, unchanged: a fleet broker that reached into a host's
        residency would be the second master this design exists not to have.

        The grant carries a `lease_id`. The caller heartbeats it and releases it;
        a dead caller's lease expires on `LIVESTACK_LEASE_TTL_S`, because a
        client that crashed cannot be relied on to hand capacity back.
        """
        from .fleet_admit import admit as _admit
        from .fleet_auth import AuthError, authenticate
        from .ledger import new_decision_id
        kind = payload.get("kind")
        if not kind:
            raise HTTPException(400, "'kind' required")
        # WHO, before anything else. The quota is worth exactly what `owner` is
        # worth, and until this existed `owner` was a string in the body of an
        # unauthenticated POST — an account raised its own ceiling by renaming
        # itself. The credential decides; the body is consulted only for a
        # principal that was granted the right to delegate, and only inside its
        # prefix.
        principals = getattr(broker, "fleet_principals", None)
        if principals is not None:
            try:
                owner, principal = authenticate(
                    principals, authorization, payload.get("owner"))
            except AuthError as e:
                raise HTTPException(e.status, e.detail)
        else:
            # Auth not configured. Today's behaviour, and the startup line and
            # GET /fleet both say so — because a quota over an unauthenticated
            # owner is decorative, and that has to be visible rather than
            # discovered.
            owner, principal = (payload.get("owner", "consumer"), None)
        est = (payload.get("estimate") or {}).get("duration_s", 60.0)
        # WHERE the work may run, stated by the caller and APPLIED here — the
        # same policy shape `GET /fleet/rank?regions=` accepts. Filtering
        # before scheduling (not scoring after) is what guarantees the answer
        # never places outside the policy: a region the caller refused cannot
        # win, place second and get chosen by default, or appear in the
        # granted target. `/fleet/rank` already moved callers onto this
        # contract; admission silently dropping the guarantee on the way in
        # was the gap (see `_plans/fleet-caller-identity.md`, requirement
        # "Region policy holds on the admission path").
        view = broker.fleet_view()
        wanted = [r.strip().lower()
                  for r in str(payload.get("regions") or "").split(",") if r.strip()]
        allow_unknown = bool(payload.get("allow_unknown_region"))
        region_policy = None
        if wanted:
            from .client import eligible_targets
            # eligible_targets speaks the ranker's row shape: one row per node
            # with its declared region. Unknown region is excluded unless the
            # caller explicitly allows it — silence is not a match.
            rows = [{"target_id": facade_id(n.get("peer", "")),
                     "region": n.get("region")}
                    for h in (view.get("hosts") or {}).values()
                    for n in (h.get("nodes") or [])]
            kept, rejected = eligible_targets(
                {"targets": rows}, allow_regions=set(wanted),
                allow_unknown_region=allow_unknown)
            kept_ids = {r["target_id"] for r in kept}
            # Filter into a COPY: the view belongs to the broker, and the
            # next caller (or the same caller retrying) must see the fleet
            # as it is, not as the last request's policy left it.
            view = {
                **view,
                "hosts": {
                    host_id: {**h, "nodes": [
                        n for n in (h.get("nodes") or [])
                        if facade_id(n.get("peer", "")) in kept_ids]}
                    for host_id, h in (view.get("hosts") or {}).items()
                },
            }
            region_policy = {"allow": wanted,
                             "allow_unknown": allow_unknown,
                             "rejected": rejected}
        # Minted BEFORE deciding: it seeds exploration (Jingway design §5.1),
        # and it is the join key from this admit's ledger record to its policy
        # decision, its lease, and the lease's outcome.
        decision_id = new_decision_id()
        runtime = getattr(broker, "policy_runtime", None)
        result = _admit(
            view, kind=kind,
            # From the broker's own lease ledger, expired entries dropped
            # first — a quota computed over leases nobody heartbeats would turn
            # one caller's crash into an outage that outlives it.
            usage=broker.owner_usage(),
            sla=payload.get("sla", "normal"),
            owner=owner,
            selector=payload.get("selector") or {},
            locality_host=payload.get("locality_host"),
            vantage=payload.get("via") or payload.get("vantage") or "direct",
            estimate_s=float(est),
            policy=getattr(broker, "fleet_policy", None),
            runtime=runtime, decision_id=decision_id,
        )
        decision = result.pop("policy_decision", None)
        pointer = None
        if runtime is not None:
            # Only a committed choice is recorded; a refusal (no target, or a
            # quota refusal, which never reached the choice) is only counted.
            runtime.record_decision(decision, principal=(
                principal.name if principal else None))
            if (runtime.recorder is not None and decision is not None
                    and decision.get("chosen") is not None):
                # The ledger's pointer into the policy stream. Absent when the
                # stream is unavailable, so it never names a record that was
                # never written (a dropped one is counted as a gap instead).
                pointer = {"decision_id": decision_id,
                           "artifact_version": decision["artifact_version"],
                           "chosen": decision["chosen"],
                           "explored": bool(decision["explored"])}
        if region_policy is not None:
            result["region_policy"] = region_policy
            if (not result.get("granted")
                    and result.get("refused") != "account_quota"
                    and region_policy["rejected"]):
                # A refusal names the excluded targets, the same sentence
                # /fleet/rank uses: "no llm target in na: http://cn (region
                # cn, wanted na)". A caller must be able to tell "the fleet
                # has no room" from "the room exists and your policy refused
                # it" — those want opposite responses.
                result["reason"] = (
                    f"no {kind} target in {'/'.join(region_policy['allow'])}: "
                    + "; ".join(f"{r['target_id']} ({r['why']})"
                                for r in region_policy["rejected"][:4]))
        request = {"owner": owner,
                   # The principal is recorded BESIDE the owner, not instead of
                   # it: "the hub, acting for acct_x" and "acct_x itself" are
                   # different facts, and a retrospective that cannot tell them
                   # apart cannot answer who actually spent the capacity.
                   "principal": principal.name if principal else None,
                   "sla": payload.get("sla", "normal"),
                   "vantage": result.get("vantage"),
                   "selector": payload.get("selector") or {},
                   "locality_host": payload.get("locality_host"),
                   # The region policy in force and every row it rejected, so
                   # the admit record shows the placement the policy forbade
                   # beside the one it allowed — the same shape /fleet/rank
                   # records.
                   "region_policy": region_policy}
        # UNMET DEMAND. Recorded only for a capacity refusal: an account at its
        # ceiling does not need a bigger fleet, and renting one would not admit
        # its next job either. See `fleet_demand` for why this is a decaying
        # signal rather than a queue.
        demand = getattr(broker, "fleet_demand", None)
        if (demand is not None and not result.get("granted")
                and result.get("refused") != "account_quota"):
            demand.record(
                kind=kind, owner=owner, sla=payload.get("sla", "normal"),
                regions=wanted, selector=payload.get("selector") or {},
                est_duration_s=float(est),
                reason=result.get("reason") or "")
        if result.get("refused") == "account_quota":
            # 429, not 200-with-no-target: an account at its ceiling is a
            # different answer from a full fleet, and a caller that cannot tell
            # them apart retries forever against a fleet that will never say yes.
            broker.emit_admit(result, request)
            raise HTTPException(429, result.get("reason") or "account quota")
        lease_id = None
        target = result.get("target")
        if target:
            # The ledger is bookkeeping and the grant is the product: a checkout
            # failure must not void an answer the caller already has.
            try:
                lease_id = broker.hosted_checkout(
                    target["target_id"], kind, request["owner"],
                    decision_id=decision_id if pointer else None)
            except Exception as e:
                print(f"[harmony] fleet lease checkout failed: {e}", flush=True)
        if pointer is not None:
            broker.emit_admit(result, request, lease_id, policy=pointer)
        else:
            broker.emit_admit(result, request, lease_id)
        out = {k: v for k, v in result.items() if k != "candidates"}
        out["lease_id"] = lease_id
        return out


    # --- the scheduler policy artifact (scheduler-policy-routine design §6) --
    def _policy_runtime(policy_id: str):
        from .policy_runtime import POLICY_ID
        if policy_id != POLICY_ID:
            raise HTTPException(404, f"no policy {policy_id!r}; this broker serves {POLICY_ID}")
        runtime = getattr(broker, "policy_runtime", None)
        if runtime is None:
            raise HTTPException(503, "this broker runs no policy runtime (fleet broker only)")
        return runtime

    def _policy_admin(authorization):
        """Publishing changes routing for everyone: it needs an authenticated
        principal carrying `policy_admin`, and auth OFF refuses outright."""
        from .fleet_auth import POLICY_ADMIN, AuthError, bearer_token, principal_for
        principals = getattr(broker, "fleet_principals", None)
        if principals is None:
            raise HTTPException(403, "policy publishing requires fleet auth")
        try:
            who = principal_for(principals, bearer_token(authorization))
        except AuthError as e:
            raise HTTPException(e.status, e.detail)
        if not who.can(POLICY_ADMIN):
            raise HTTPException(403, f"'{who.name}' does not carry {POLICY_ADMIN}")
        return who

    @app.put("/fleet/policy/{policy_id}")
    def fleet_policy_put(policy_id: str, role: str = "active",
                         payload: Any = Body(...), authorization: str = Header(None)):
        """Publish an artifact (``role=active``) or up to two shadows
        (``role=shadow``, a JSON array). Validated by the native module, which
        recomputes the version; written atomically, previous kept."""
        from .policy_runtime import PolicyRouteError
        _policy_admin(authorization)
        runtime = _policy_runtime(policy_id)
        try:
            return runtime.publish(role, payload)
        except PolicyRouteError as e:
            raise HTTPException(e.status, e.detail)

    @app.get("/fleet/policy/{policy_id}")
    def fleet_policy_get(policy_id: str):
        return _policy_runtime(policy_id).status()

    @app.post("/fleet/policy/{policy_id}/revert")
    def fleet_policy_revert(policy_id: str, authorization: str = Header(None)):
        """Swap the previous artifact back in. A file swap: no model, no
        improver, nothing a bad policy can lock out."""
        from .policy_runtime import PolicyRouteError
        _policy_admin(authorization)
        runtime = _policy_runtime(policy_id)
        try:
            return runtime.revert()
        except PolicyRouteError as e:
            raise HTTPException(e.status, e.detail)

    # --- the provisioning control plane (v1) --------------------------------
    #
    # Three routes with a strict division of labour: /fleet/plan READS,
    # /fleet/operations SPENDS, GET /fleet/operations/{id} OBSERVES. See
    # `fleet_ops_api` for why they are separate from /fleet/admit, and
    # `openspec/specs/fleet-provisioning-operations/` for the requirements
    # they implement.
    def _ops_store():
        store = getattr(broker, "operation_store", None)
        if store is None:
            # Not configured is a 501, never an empty answer: a loop told "no
            # operations" by a broker that has no store would conclude the fleet
            # is idle and keep planning against a control plane that is not there.
            raise HTTPException(501, "this broker has no operation store "
                                     "(set LIVESTACK_LEDGER_DIR / run as a "
                                     "fleet broker)")
        return store

    def _ops_owner(authorization, payload):
        from .fleet_auth import AuthError, authenticate
        principals = getattr(broker, "fleet_principals", None)
        if principals is None:
            return payload.get("owner", "consumer"), None
        try:
            return authenticate(principals, authorization, payload.get("owner"))
        except AuthError as e:
            raise HTTPException(e.status, e.detail)

    @app.post("/fleet/plan")
    def fleet_plan(payload: dict = Body(default={}), authorization: str = Header(None)):
        """The whole plan for a caller-supplied queue. A READ: it reserves
        nothing, so calling it twice costs nothing and changes nothing.

        It reports what `/fleet/admit` never could: the actions for every job at
        once, the pools that were excluded and why, the reservations already
        outstanding, and the inputs the fleet did not actually know."""
        from .fleet_ops_api import build_plan
        owner, principal = _ops_owner(authorization, payload)
        regions = tuple(r.strip().lower()
                        for r in str(payload.get("regions") or "").split(",")
                        if r.strip())
        # The caller's own queue, plus the demand the broker itself could not
        # place. Without the second half a job answered with `Queue` never
        # reaches a plan, and a burst the scheduler would authorise never
        # happens — the seam this closes.
        jobs = list(payload.get("jobs") or [])
        if payload.get("include_demand", True) and getattr(broker, "fleet_demand", None):
            known = {str(j.get("job_id")) for j in jobs}
            jobs += [j for j in broker.fleet_demand.jobs()
                     if j["job_id"] not in known]
        return build_plan(
            broker.fleet_view(), jobs, owner=owner,
            policy=getattr(broker, "fleet_policy", None) or SchedulerPolicy(),
            pools=getattr(broker, "fleet_pools", ()),
            store=_ops_store(), usage=broker.owner_usage(),
            allow_regions=regions,
            vantage=payload.get("via") or payload.get("vantage") or "direct",
            runtime=getattr(broker, "policy_runtime", None))

    @app.post("/fleet/operations")
    def fleet_operation(payload: dict = Body(...), authorization: str = Header(None)):
        """Claim, then act. The claim is durable and synchronous; the provider
        call is not, so losing this reply costs nothing — the operation is
        already on disk and already counted against its owner's quota."""
        from .fleet_ops_api import (ActionRefused, deprovision, plan_is_current,
                                    provision)
        store = _ops_store()
        owner, principal = _ops_owner(authorization, payload)
        action = payload.get("action") or {}
        pools = getattr(broker, "fleet_pools", ())
        policy = getattr(broker, "fleet_policy", None) or SchedulerPolicy()
        runtime = getattr(broker, "policy_runtime", None)
        stale = plan_is_current(payload.get("plan_version") or "", policy=policy,
                                pools=pools, now=time.time(),
                                max_age_s=float(os.environ.get(
                                    "LIVESTACK_PLAN_MAX_AGE_S", "120")),
                                artifact_version=(runtime.artifact_version
                                                  if runtime is not None else None))
        if stale:
            raise HTTPException(409, stale)
        kind = str(action.get("type") or "")
        try:
            if kind == "provision":
                key = str(payload.get("idempotency_key") or "").strip()
                if not key:
                    # Refused rather than generated. A key the broker invented is
                    # a key the caller cannot repeat, which is the same as having
                    # none the moment a reply is lost.
                    raise HTTPException(400, "'idempotency_key' is required for a "
                                             "provision; the caller must choose it "
                                             "so it can repeat it")
                return provision(
                    action, store=store, pools=pools,
                    providers=getattr(broker, "fleet_providers", {}),
                    owner=owner, principal=principal.name if principal else None,
                    plan_version=payload.get("plan_version") or "",
                    idempotency_key=key, policy=policy,
                    usage=broker.owner_usage(),
                    announce_env=getattr(broker, "fleet_worker_env", {}),
                    allow_regions=tuple(
                        r.strip().lower()
                        for r in str(payload.get("regions") or "").split(",")
                        if r.strip()),
                    allow_unknown_region=bool(payload.get("allow_unknown_region")),
                    log=lambda m: print(m, flush=True))
            if kind == "deprovision":
                return deprovision(
                    action, store=store,
                    providers=getattr(broker, "fleet_providers", {}),
                    busy=lambda node: _drain_blocked(broker, node),
                    log=lambda m: print(m, flush=True))
        except ActionRefused as e:
            raise HTTPException(e.status, e.detail)
        raise HTTPException(400, f"action type {kind!r} is not one of "
                                 f"'provision' / 'deprovision'")

    @app.get("/fleet/ledger")
    def fleet_ledger(since: float = 0.0, limit: int = 200, kind: str = None):
        """Decision records, newest last — the retrospective, over HTTP.

        Read-only and deliberately narrow. It exists because the supervision
        loop's repair surface includes "read what this broker decided recently",
        and a repair turn that had to ssh into the broker to answer that would
        be a repair surface in name only. The records carry no secrets by
        construction (see `ledger.py`); `owner` is an id.
        """
        led = getattr(broker, "ledger", None)
        if led is None:
            raise HTTPException(501, "this broker writes no decision ledger "
                                     "(LIVESTACK_LEDGER=0)")
        return {"records": led.read(since=since or None, kind=kind,
                                    limit=max(1, min(int(limit), 1000))),
                "path": led.path}

    @app.get("/fleet/operations")
    def fleet_operations():
        """Every operation that is not terminal — what a supervision tick
        supervises, and what an operator reads when a burst went wrong."""
        return {"operations": [op.to_dict() for op in _ops_store().active()]}

    @app.get("/fleet/operations/{operation_id}")
    def fleet_operation_state(operation_id: str):
        """The correlated facts a gate reads: the state, the receipts, the
        structured error. Never the HTTP status of the dispatch that started it."""
        op = _ops_store().get(operation_id)
        if op is None:
            raise HTTPException(404, f"no operation {operation_id!r}")
        return op.to_dict()

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
                    # Time the links to the other hosts. Cheap (one /peers GET
                    # each) and it is the only distance signal that is not
                    # measured from this broker's own vantage.
                    broker.measure_links()
                    # Correlate: a node carrying an operation's id and
                    # reporting ready is the only thing that greens that
                    # operation. Cheap (it reads the view the tick already
                    # built) and it must happen on the broker's own clock —
                    # an operation whose supervision loop is down still has to
                    # stop being a pending create.
                    _settle_operations(broker)
                    p = broker.plan_and_apply([], state["last_evicted_at"])
                    _track(p)
                    if p.of(Evict) or p.of(Load):
                        print(f"[harmony] reconcile: {p.summary()}", flush=True)
                except Exception as e:  # a peer down etc. — keep looping
                    print(f"[harmony] reconcile error: {e}", flush=True)

        threading.Thread(target=_reconcile_loop, name="harmony-reconcile",
                         daemon=True).start()

    return app


def _release_report(body: dict) -> dict:
    """``hosted_release`` keywords from a release body, or HTTP 422."""
    import math
    from fastapi import HTTPException
    problems = []
    out = {}
    if "status" in body:
        if body["status"] not in ("ok", "failed"):
            problems.append(f"status must be \"ok\" or \"failed\", got {body['status']!r}")
        else:
            out["caller_ok"] = body["status"] == "ok"
    if "wall_s" in body:
        w = body["wall_s"]
        if (isinstance(w, bool) or not isinstance(w, (int, float))
                or not math.isfinite(w) or w < 0):
            problems.append(f"wall_s must be a non-negative number, got {w!r}")
        else:
            out["job_wall_s"] = float(w)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    return out


def _settle_operations(broker) -> None:
    """Green the operations whose node has announced, and fail the ones whose
    deadline passed. Both on the broker's own tick, so an operation's fate does
    not depend on the supervision loop being alive.

    Silence is not success and it is not failure either: an operation that is
    neither correlated nor expired simply stays where it is, still counted, and
    `GET /fleet/operations` still shows it.
    """
    store = getattr(broker, "operation_store", None)
    if store is None:
        return
    from .fleet_workers import announce_from_view
    for op in announce_from_view(store, broker.fleet_view()):
        print(f"[fleet] {op.operation_id} announced as {op.node_id}", flush=True)
    for op in store.expire():
        print(f"[fleet] {op.operation_id} FAILED: {op.reason} "
              f"(instance {op.provider_instance_id or 'unknown'} may still be "
              f"billing)", flush=True)


def _drain_blocked(broker, node_id: str) -> Optional[str]:
    """Why this node may not be released, or None.

    Three independent reasons, each named rather than folded into a boolean:
    a live lease, a membership row that still says the node is serving, and the
    node simply not being one this broker can see. The last one matters — a node
    that has fallen out of the view is not evidence that it is empty, it is
    evidence that we cannot tell, and "cannot tell" must not read as "safe".
    """
    held = broker.leases_on(node_id)
    if held:
        return f"{held} active lease(s) on {node_id}"
    view = broker.fleet_view()
    row = next((n for h in (view.get("hosts") or {}).values()
                for n in (h.get("nodes") or [])
                if facade_id(n.get("peer", "")) == node_id), None)
    if row is None:
        return (f"{node_id} is not in the fleet view; its state is unknown, "
                f"which is not the same as empty")
    in_flight = (row.get("load") or {}).get("in_flight")
    if isinstance(in_flight, (int, float)) and in_flight > 0:
        return f"{node_id} reports in_flight={in_flight}"
    return None


def _node_control_token_from_env(env=None) -> Optional[str]:
    """Read hostd's outbound node-control credential without putting it in argv.

    A configured file must be private and contain one non-empty token. Failure is
    fatal: silently dropping the credential turns broker dispatch into repeated
    401s while admission still appears to work.
    """
    from pathlib import Path
    source = os.environ if env is None else env
    filename = (source.get("LIVESTACK_NODE_CONTROL_TOKEN_FILE") or "").strip()
    if not filename:
        return None
    path = Path(filename)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(f"node control token file must be mode 0600: {path}")
    token = path.read_text().strip()
    if not token or "\n" in token:
        raise RuntimeError(f"node control token file must contain one token: {path}")
    return token


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
    #
    # The explicit opt-out is "none"/"off"/"-": a peerless broker (the build
    # broker — only hosted devices, no model nodes at all) should not spend its
    # reconcile loop probing three localhost guesses that can never answer.
    peers_env = os.environ.get("LIVESTACK_PEERS", "").strip()
    if peers_env.lower() in ("none", "off", "-"):
        peer_urls = []
    elif peers_env:
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
    # apply (default) = host broker; observe = fleet broker. The default is what
    # keeps every existing deployment byte-for-byte unchanged.
    dispatch = os.environ.get("LIVESTACK_DISPATCH", "apply").strip().lower() != "observe"
    port = int(os.environ.get("LIVESTACK_BROKER_PORT", "8799"))
    host_id = os.environ.get("LIVESTACK_HOST_ID", "").strip() or _hostname()
    emitter_id = f"{host_id}:{port}"
    from .ledger import ledger_from_env
    ledger = ledger_from_env(
        "decisions-" + host_id if dispatch else "fleet-decisions",
        # The fleet broker sees every host, so it writes more: 64 MiB x 4 to the
        # host broker's 32 x 4. Both are bounded by the same writer; see rule 10
        # and `_plans/decision-ledger.md` §6.
        32 if dispatch else 64,
        log=lambda m: print(m, flush=True),
    )
    link_env = os.environ.get("LIVESTACK_LINK_PEERS", "").strip()
    link_peers = [u.strip().rstrip("/") for u in link_env.split(",") if u.strip()]
    # A MALFORMED QUOTA MUST NOT TAKE THE BROKER DOWN.
    #
    # Found the hard way on 2026-09-05: systemd strips the double quotes out of
    # `Environment={"a": 1}`, so the value arrived as `{a: 1}`, `json.loads`
    # raised at import, and the fleet broker crash-looped. The typo was mine;
    # the crash was the code's. Per-account quotas are the setting that changes
    # most often — a tenant arrives, a tenant leaves — so it is the one most
    # likely to be mistyped, and a fleet-wide outage is a wildly disproportionate
    # answer to a missing quote.
    #
    # It degrades LOUDLY rather than silently, and `/fleet` reports what is
    # actually in force, because a config error that quietly disables a safety
    # control is the failure this is trying to avoid in the first place.
    def _quota_int(name):
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print(f"[harmony] {name}={raw!r} is not a number — NO CEILING is in "
                  f"force; GET /fleet reports the effective quota", flush=True)
            return None

    def _quota_map(name):
        raw = os.environ.get(name, "").strip()
        if not raw:
            return {}
        try:
            got = json.loads(raw)
            if not isinstance(got, dict):
                raise ValueError("not an object")
            return {str(k): int(v) for k, v in got.items()}
        except Exception as e:
            print(f"[harmony] {name}={raw!r} is not a JSON object of "
                  f"account->int ({e}) — per-account OVERRIDES are ignored; the "
                  f"fleet-wide ceiling still applies. NOTE systemd strips double "
                  f"quotes: write Environment='{name}={{\"acct\": 3}}'",
                  flush=True)
            return {}

    from .fleet_auth import RELAY_ANY, principals_from_env
    fleet_principals = principals_from_env(log=lambda m: print(m, flush=True))
    fleet_policy = SchedulerPolicy(
        max_concurrent_per_account=_quota_int("LIVESTACK_ACCOUNT_QUOTA"),
        account_quotas=_quota_map("LIVESTACK_ACCOUNT_QUOTAS"),
        fair_share_penalty_s=float(
            os.environ.get("LIVESTACK_FAIR_SHARE_PENALTY_S", "30") or 30),
    )
    if fleet_principals is None:
        # Auth off is a STATE, not an absence — said out loud at startup and
        # reported by GET /fleet, because the day tokens flip on nobody should
        # have to diff journals to notice.
        print("[harmony] fleet auth is OFF — no principal source configured "
              "(LIVESTACK_FLEET_TOKENS_FILE or LIVESTACK_FLEET_TOKENS); "
              "`owner` comes from the request body", flush=True)
        if fleet_policy.max_concurrent_per_account is not None:
            # The one combination that is quietly useless: a ceiling counted
            # against an owner any caller can choose. Said loudly rather than
            # left to be discovered by whoever eventually reads the ledger.
            print("[harmony] WARNING account quota is set but fleet auth is "
                  "OFF — `owner` comes from the request body, so the quota is "
                  "advisory and any caller can spend any account's capacity",
                  flush=True)
    elif fleet_principals:
        print(f"[harmony] /admit and /fleet/admit require a bearer token; "
              f"{len(fleet_principals)} principal(s): "
              + ", ".join(sorted(
                  f"{p.name}"
                  + (f"->{p.owner}" if p.owner
                     else "->ANY OWNER" if p.delegate_prefix == RELAY_ANY
                     else f"->{p.delegate_prefix}*")
                  for p in fleet_principals.values())), flush=True)
    else:
        # A source was configured but yielded nothing (refused file, malformed
        # JSON, every entry rejected). Failing closed, per load_principals'
        # own promise — this is an alarm state, not "auth off".
        print("[harmony] fleet auth is ON but the principal table is EMPTY "
              "— every caller gets 401 until the token source is fixed. A "
              "broker that cannot identify anyone must refuse everyone, not "
              "admit anyone", flush=True)
    print(f"[harmony] account quota: "
          f"{fleet_policy.max_concurrent_per_account or 'NO CEILING'}"
          f"{f' (overrides: {dict(fleet_policy.account_quotas)})' if fleet_policy.account_quotas else ''}"
          f", fair-share penalty {fleet_policy.fair_share_penalty_s:.0f}s",
          flush=True)
    node_control_token = _node_control_token_from_env()
    broker = build_broker(
        peer_urls, device_config=device_config,
        default_vram_gb=float(os.environ.get("LIVESTACK_VRAM_GB", "24")),
        default_reserved_gb=float(os.environ.get("LIVESTACK_RESERVED_GB", "2")),
        membership=membership,
        extra_units=extra_units,
        dispatch=dispatch, ledger=ledger, emitter_id=emitter_id,
        node_control_token=node_control_token,
    )
    broker.node_control_token = node_control_token
    broker.host_id = host_id
    broker.link_peers = link_peers
    broker.fleet_policy = fleet_policy
    broker.fleet_principals = fleet_principals
    if not dispatch:
        # The target choice as a compiled policy (scheduler-policy-routine).
        # Fleet broker only: it is the one process whose /fleet/admit places
        # jobs fleet-wide, and the one whose choices are recorded.
        from .policy_runtime import PolicyRuntime
        broker.policy_runtime = PolicyRuntime.from_env(
            log=lambda m: print(m, flush=True))
        _ps = broker.policy_runtime.status()
        print(f"[policy] {_ps['policy_id']}: source={_ps['source']} "
              f"version={_ps['active']['version']} mode={_ps['mode']} "
              f"native={_ps['native']} records="
              f"{_ps['records']['unavailable'] or _ps['records']['stream']}"
              + (f" DEGRADED: {', '.join(_ps['degraded'])}" if _ps["degraded"] else ""),
              flush=True)

    # --- the provisioning control plane -------------------------------------
    #
    # Pools are an OPERATOR statement (which machines, where, at what price, how
    # many) — livestack cannot discover willingness or price. With none declared
    # the broker plans and admits exactly as before and can never provision,
    # which is the correct behaviour for every host broker and for a fleet
    # broker nobody has given a budget to.
    import json as _json
    from .fleet_demand import DemandRegister
    from .fleet_operations import store_from_env
    from .fleet_ops_api import providers_from_env
    from .fleet_pools import parse_pools
    _say = lambda m: print(m, flush=True)  # noqa: E731
    broker.fleet_pools = parse_pools(
        os.environ.get("LIVESTACK_FLEET_POOLS", ""), log=_say)
    broker.fleet_providers = providers_from_env(broker.fleet_pools, log=_say)
    # What a provisioned worker needs in its environment to find its way home.
    # `LIVESTACK_OPERATION_ID` is added per operation by the runner; everything
    # else is operator config, because only the operator knows which address of
    # this broker a machine in another datacentre can actually reach.
    try:
        broker.fleet_worker_env = {
            str(k): str(v) for k, v in
            _json.loads(os.environ.get("LIVESTACK_FLEET_WORKER_ENV", "") or "{}").items()}
    except Exception as e:
        broker.fleet_worker_env = {}
        _say(f"[fleet] LIVESTACK_FLEET_WORKER_ENV is malformed ({e}); provisioned "
             f"workers will boot with NO broker address and will never announce")
    broker.fleet_demand = DemandRegister(
        ttl_s=float(os.environ.get("LIVESTACK_DEMAND_TTL_S", "120")),
        max_entries=int(os.environ.get("LIVESTACK_DEMAND_MAX", "256")),
        log=_say)
    _say(f"[fleet] demand register: {broker.fleet_demand.max_entries} shape(s), "
         f"{broker.fleet_demand.ttl_s:.0f}s TTL (a decaying signal, not a queue: "
         f"a caller that stops asking stops counting)")
    broker.operation_store = store_from_env(
        ledger=ledger, emitter_id=emitter_id, log=_say)
    _say(f"[fleet] operation store -> {broker.operation_store.path} "
         f"(bound: {broker.operation_store.max_records} records"
         + (f", {broker.operation_store.max_age_s / 86400:.0f}d"
            if broker.operation_store.max_age_s else ", age window disabled") + ")")
    # A pool the adapter cannot actually build from is reported AT STARTUP, not
    # at the first burst. The alternative is a claimed operation that holds its
    # owner's quota and then fails on a parameter that was missing all along.
    from .fleet_pools import spec_for as _spec_for
    for _pool in broker.fleet_pools:
        _adapter = broker.fleet_providers.get(_pool.provider)
        if _adapter is None:
            continue
        _problems = _adapter.validate_spec(
            _spec_for(_pool, announce_env=broker.fleet_worker_env))
        if _problems:
            _say(f"[fleet] pool {_pool.id!r} CANNOT PROVISION: "
                 + "; ".join(_problems)
                 + ". It will be planned and every create will be refused as "
                   "request_or_workload_fault. Fix the declaration.")
        elif _pool.tier.name == "SPOT":
            _say(f"[fleet] pool {_pool.id!r} buys SPOT (SpotStrategy=SpotAsPriceGo)"
                 + (f", price limit ¥{_pool.spot_price_limit}/h"
                    if _pool.spot_price_limit is not None else ", provider cap"))

    # RECONCILE BEFORE SERVING. An operation caught mid-create by the last
    # shutdown is resolved by asking the provider, never by creating again —
    # and until it is resolved it keeps holding its owner's quota. Doing this
    # after the port is open would let a claim race a create whose outcome is
    # still unknown.
    from .fleet_workers import recover_all
    recover_all(broker.operation_store, broker.fleet_providers, log=_say)

    import uvicorn
    role = "host broker" if dispatch else "fleet broker (observe-only)"
    print(f"[harmony] {role} on :{port} as {host_id} over {len(broker.peers)} "
          f"seeded peers and {len(link_peers)} link peers "
          f"(nodes self-register at POST /peers; GET /peers shows membership; "
          f"GET /fleet shows the fleet)", flush=True)
    if ledger is not None:
        print(f"[harmony] decision ledger -> {ledger.path} "
              f"({ledger.max_bytes // (1024 * 1024)} MiB x {ledger.max_files}"
              + (f", {ledger.max_age_s / 86400:.0f}d" if ledger.max_age_s
                 else ", age window disabled") + ")", flush=True)
    uvicorn.run(build_app(broker), host="0.0.0.0", port=port)


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "host"


if __name__ == "__main__":
    main()
