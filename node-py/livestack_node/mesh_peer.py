"""mesh_peer.py — MeshPeer: the broker's Peer duck type over the meshlink relay stack.

Phase 5 of the meshlink transport backbone (openspec change
``meshlink-transport-backbone``). A mesh peer is addressed by its roster URL

    mesh://<realm>/<daemon_id>/<facade-prefix>     e.g.  mesh://livestack/gpu-box-7/livestack

which is an opaque URL-keyed record to the broker exactly like an
``http://host:port/livestack`` RestPeer: planning, membership and pruning never
look inside it (design.md "How ids cross the boundary").

**Identity (DR-2).** The node's identity is ``realm + daemon_id`` — the
daemon_id the operator assigned, carried in the ``bdrt1`` attachment's daemon
field and named by the URL. The ed25519 daemon key only proves possession at
the relay door; rotating it or reconnecting must not change who the broker
thinks this peer is. :attr:`MeshPeer.node_id` therefore returns the URL-derived
identity and never the node's self-reported ``hostname:port``, which is the
loopback facade's address and has no meaning across a tunnel.

**The dial.** One WSS stream per request, exactly mirroring the target side's
``mesh_outbound_py`` HTTP connector so both tunnel halves speak the same
``ls-h1`` envelope (JSON head: method/path/headers + body framing). The relay's
``connectClient`` door gives the caller a RAW byte pipe — it wraps the caller's
bytes into DATA frames for the target and strips the target's frames back to
payloads — so the caller writes the request head and body as plain messages and
reads the response envelope out of the reply stream. The door URL is

    <relay url><route_prefix>/<route>/<daemon_id>?cap=<bdsr1>

where ``<route>`` is the facade-prefix segment of the peer URL and the ``bdsr1``
caller capability is minted per dial from :mod:`relay_control`'s cap key ring
(DR-1, scope ``terminal.proxy`` — the relay engine's daemon door checks that
scope).

**Route selection.** Relay candidates come from the relay control config
(``LIVESTACK_RELAY_URLS`` via :class:`relay_control.RelayConfig`) and are
ranked by the mesh-route-py Picker — the canonical route policy, imported, not
reimplemented. Outcomes are recorded back (transport failure, task latency,
success) so a dead relay cools down and the next candidate is tried within the
same request's timeout budget.

**Failure semantics (jidoka, design.md).** A dead tunnel raises
:class:`MeshTunnelDown` (degradation ``mesh_tunnel_down``); the relay door's
HTTP 429 (per-account quota, DR-3) raises :class:`RelayQuotaExceeded`
(degradation ``relay_quota``) and is deliberately NOT failed over to another
relay — quota binds the account, not the route. Every failure is named in the
exception text; nothing hangs past the caller's timeout and nothing returns an
empty success.

The meshlink packages (``mesh_outbound_py`` mux codec + ls-h1 envelope, and the
``mesh_route_py`` pyo3 Picker) come from the sibling checkout pinned by
``MESHLINK.lock`` (DR-5); the tests put them on ``sys.path`` the same way
``test_relay_control.py`` resolves the relay dist. Importing this module
without them fails loudly at :class:`MeshPeer` construction, never at first
dial.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from . import relay_control
from .hostbroker import RestPeer

mesh_route_py = None
ls_h1 = None
websockets = None
FRAME_LIMIT = 4 * 1024 * 1024
_MESHLINK_IMPORT_ERROR: Optional[BaseException] = None


def _import_meshlink() -> None:
    """Import the meshlink packages, on first MeshPeer construction rather than
    at module import: the sibling checkout's packages land on sys.path at
    different times (tests resolve MESHLINK_REPO before importing livestack
    modules; other hosts install the wheels), and an import that ran too early
    must get a second chance, not a permanent None.

    The caller door is a raw byte pipe (meshlink tunnels.ts connectClient), so
    MeshPeer speaks ls-h1 over plain messages — but body chunking still rides
    the mux codec's FRAME_LIMIT, so both tunnel halves agree on the wire's
    frame bound."""
    global mesh_route_py, ls_h1, websockets, FRAME_LIMIT, _MESHLINK_IMPORT_ERROR
    if mesh_route_py is not None and websockets is not None:
        return
    try:
        import mesh_route_py as _mrp
        from mesh_outbound_py import mux as _mux
        from mesh_outbound_py.connectors import ls_h1 as _ls_h1
        import websockets as _ws
    except ImportError as e:
        _MESHLINK_IMPORT_ERROR = e
        raise RuntimeError(
            "meshlink packages are not importable (mesh_route_py, "
            "mesh_outbound_py) — the meshlink checkout must be on sys.path; "
            f"original error: {e}")
    mesh_route_py, ls_h1, websockets = _mrp, _ls_h1, _ws
    FRAME_LIMIT = _mux.FRAME_LIMIT
    _MESHLINK_IMPORT_ERROR = None

MESH_SCHEME = "mesh"

#: Scope the relay engine's daemon door verifies caller capabilities against
#: (meshlink server.ts `authorizeDaemon` — hard-coded there, so this names it).
DAEMON_DOOR_SCOPE = "terminal.proxy"

#: The livestack realm's route-prefix cosmetic (DR-4) — the prefix the relay
#: strips before route matching, served per-realm by the pinned meshlink build
#: (MESHLINK.lock). RelayRoute.route_prefix defaults to it; a relay serving
#: the livestack realm under a different prefix is configured with that prefix
#: explicitly.
LIVESTACK_ROUTE_PREFIX = relay_control.DEFAULT_ROUTE_PREFIX


# ---------------------------------------------------------------------------
# Named degradations
# ---------------------------------------------------------------------------

class MeshPeerError(RuntimeError):
    """Base for mesh dial failures. ``degradation`` is the stable name design.md
    assigns the class, so a probe failure can be attributed without parsing
    message text."""
    degradation = "mesh_peer_error"


class MeshTunnelDown(MeshPeerError):
    """The tunnel could not be established or dropped before the response
    completed. Degradation: ``mesh_tunnel_down``."""
    degradation = "mesh_tunnel_down"


class RelayQuotaExceeded(MeshPeerError):
    """The relay refused the dial with HTTP 429 — the per-realm/account quota
    (DR-3) bound before the node was ever asked. Distinct from node unhealth;
    deliberately NOT failed over to another relay, because the quota keys on
    the caller's account, which is the same at every relay. Degradation:
    ``relay_quota``."""
    degradation = "relay_quota"


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def parse_mesh_url(url: str) -> Tuple[str, str, str]:
    """``mesh://livestack/gpu-box-7/livestack`` -> ``(realm, daemon_id, facade_prefix)``.

    A mesh URL MUST name realm, daemon_id and a non-empty facade prefix; any
    other shape is a configuration error and raises ValueError (jidoka — a
    mis-typed roster URL must not dial *some* door)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != MESH_SCHEME or not parts.netloc or not parts.path:
        raise ValueError(
            f"mesh peer URL must be mesh://<realm>/<daemon_id>/<facade-prefix>, got {url!r}")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or not segments[0]:
        raise ValueError(
            f"mesh peer URL must name a daemon_id and a facade prefix, got {url!r}")
    return parts.netloc, segments[0], "/" + "/".join(segments[1:])


def facade_id(url: str) -> str:
    """The node id a roster peer URL names, with the legacy ``/livestack``
    suffix stripped — SCHEME-AWARELY.

    The suffix convention belongs to http(s) facade URLs
    (``http://host:port/livestack``). A mesh URL's path segments are its
    identity (``mesh://livestack/<daemon_id>/livestack`` names daemon
    ``<daemon_id>`` serving its facade at ``/livestack``); stripping them would
    rewrite the record the roster, planning and the dial seam all share, so a
    mesh URL is returned unchanged. The three legacy strip sites
    (fleet_admit.py, hostd.py x2) all call this instead of their inline
    ``endswith('/livestack')`` expression."""
    if url.lower().startswith(("http://", "https://")) and url.endswith("/livestack"):
        return url[: -len("/livestack")]
    return url


@dataclass(frozen=True)
class RelayRoute:
    """One relay the mesh peer may be reached through.

    ``url`` is the WebSocket base (``ws(s)://host``); ``relay_id`` must match
    the relay's own ``relayId`` — the ``bdsr1`` cap's ``relay_id`` claim is
    verified against it, and minting a cap that names the wrong relay is a
    token that is born refused. ``route_prefix`` is the realm cosmetic the
    relay strips before route matching (meshlink DR-4).
    """
    url: str
    relay_id: str
    region: str = "local"
    route_prefix: str = LIVESTACK_ROUTE_PREFIX

    def door_url(self, route: str, daemon_id: str, cap: str) -> str:
        path = f"{self.route_prefix}/{route}/{urllib.parse.quote(daemon_id)}"
        sep = "&" if urllib.parse.urlsplit(self.url).query else "?"
        return f"{self.url}{path}?cap={urllib.parse.quote(cap, safe='')}"


# ---------------------------------------------------------------------------
# Async bridge: the Peer duck type is synchronous; the meshlink stack is asyncio.
# ---------------------------------------------------------------------------

class _LoopThread:
    """One daemon thread running one event loop; sync callers submit coroutines.

    The broker's reconcile loop is synchronous (urllib-era), so MeshPeer owns
    its own loop rather than requiring callers to be async. Concurrent
    requests run concurrently — one stream per request is the design, and the
    quota lane holds two live streams at once; the only serialized state is
    the Picker (a pyo3 object, not thread-safe), guarded per call in _dial."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mesh-peer-loop",
                                        daemon=True)
        self._started = threading.Event()
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def run(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # The caller's timeout bounds the dial; the extra margin only covers
        # scheduling, so a wedged loop still surfaces as the caller's error.
        return future.result(timeout=timeout + 15.0)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


# ---------------------------------------------------------------------------
# MeshPeer
# ---------------------------------------------------------------------------

class MeshPeer(RestPeer):
    """RestPeer's semantics over a meshlink tunnel.

    Inherits everything transport-agnostic from :class:`RestPeer` (unit
    shaping, priority precedence, placements, warm/evict contracts) by
    overriding only ``_http``: a RestPeer dial is ``_http(f"{base}/residence")``
    with an assembled URL string, so the override re-points those assembled
    URLs at the tunnel. What changes is only how bytes move; what the broker
    can ask does not.
    """

    #: The join metadata membership records carry for mesh peers
    #: (design.md "Ledger"): mesh-attributable incidents are queriable by
    #: transport without parsing URLs. RestPeer has no such attribute and its
    #: rows stay unchanged.
    transport = "mesh"

    def __init__(self, url: str, *, relays: Optional[Sequence[RelayRoute]] = None,
                 relay_config: Optional[relay_control.RelayConfig] = None,
                 account_id: str = "livestack-broker",
                 device_id: str = "broker",
                 route_prefix: str = LIVESTACK_ROUTE_PREFIX,
                 priorities: Optional[Mapping[str, int]] = None,
                 fallback_footprints: Optional[Mapping[str, int]] = None,
                 control_token: Optional[str] = None,
                 picker_factory: Optional[Callable[[], object]] = None,
                 log: Callable[[str], None] = lambda *_: None):
        if _MESHLINK_IMPORT_ERROR is not None or mesh_route_py is None:
            _import_meshlink()
        self.realm, self.daemon_id, self._facade_prefix = parse_mesh_url(url)
        if relay_config is None or relay_config.cap_ring is None:
            raise ValueError(
                "mesh peer requires a relay config with a cap key ring "
                "(LIVESTACK_RELAY_CAP_KEYS); minting no caller capability is "
                "not a supported mode")
        if not relays:
            raise ValueError(
                f"mesh peer {url!r} has no relay candidates — set LIVESTACK_RELAY_URLS")
        self._relay_config = relay_config
        self._relays = {r.url: r for r in relays}
        self._account_id = account_id
        self._device_id = device_id
        self._log = log
        super().__init__(url, priorities=dict(priorities or {}),
                         fallback_footprints=dict(fallback_footprints or {}),
                         control_token=control_token)
        # RestPeer stripped nothing (mesh URLs have no trailing slash), but the
        # Peer contract reads `base` — keep it exactly as registered.
        self.base = url
        # One Picker per peer: candidates are THIS target's relay routes.
        # `picker_factory` is the test seam (a stubPicker needs no meshlink build).
        self._picker = (picker_factory or mesh_route_py.Picker)(None)
        self._picker.set_candidates(json.dumps({"items": [
            {"key": r.url, "kind": "regional_relay", "priority": i,
             "probe_host": mesh_route_py.host_of(r.url)}
            for i, r in enumerate(sorted(relays, key=lambda r: r.url))
        ]}))
        self._loop = _LoopThread()
        self._picker_lock = threading.Lock()

    # -- identity (DR-2) -----------------------------------------------------

    @property
    def node_id(self) -> str:
        """realm + daemon_id — stable across key rotation and reconnects.

        The inherited RestPeer value would be the node's self-reported
        hostname:port, which describes the loopback facade, not the node; the
        broker's `_node_id_seen` de-duplication must key on the daemon the
        operator named, so the URL wins."""
        return f"{MESH_SCHEME}://{self.realm}/{self.daemon_id}"

    # -- RestPeer transport seam ---------------------------------------------

    def _http(self, url, body=None, timeout=5, headers=None):
        """The RestPeer dial, re-pointed at the tunnel.

        `url` is always `f"{self.base}/<facade path>"` assembled by RestPeer;
        split it back into (method, facade path, body) and run one ls-h1
        roundtrip. HTTP >= 400 is NOT special here (RestPeer/`_http` treats any
        status as a parsed body; the broker's error paths are exception-based),
        so the (status, headers, body) tuple is folded back into RestPeer's
        json-parsed-return shape."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != MESH_SCHEME or parts.netloc != self.realm:
            raise ValueError(
                f"mesh peer {self.base!r} was asked to dial {url!r} — peers dial "
                "their own URL only")
        daemon_path = f"/{self.daemon_id}"
        if not parts.path.startswith(daemon_path + "/"):
            raise ValueError(
                f"mesh peer URL {url!r} does not address daemon {self.daemon_id!r}")
        facade_path = parts.path[len(daemon_path):]
        data = json.dumps(body).encode() if body is not None else None
        method = "POST" if body is not None else "GET"
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        # No lock across the dial: concurrent requests are the whole point of
        # one stream per request (the quota lane tests two live streams at
        # once). The Picker — the only shared mutable state — is guarded
        # per-call inside _dial.
        status, _resp_headers, raw = self._loop.run(
            self._dial(method, facade_path, request_headers, data, timeout),
            timeout)
        return json.loads(raw.decode()) if raw else {}

    # -- the tunnel dial ------------------------------------------------------

    async def _dial(self, method: str, path: str,
                    headers: Mapping[str, str], body: Optional[bytes],
                    timeout: float) -> Tuple[int, Mapping[str, str], bytes]:
        body = body or b""
        if len(body) > ls_h1.MAX_BODY_LEN:
            raise ValueError(
                f"mesh dial body {len(body)} bytes exceeds the ls-h1 bound "
                f"{ls_h1.MAX_BODY_LEN}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        started = time.monotonic()
        now_ms = time.time_ns() // 1_000_000
        with self._picker_lock:
            order = self._picker.ranked(now_ms, None) or []
        if not order:
            raise MeshTunnelDown(
                f"mesh_tunnel_down: no relay candidates configured for "
                f"{self.base!r}")
        last: Optional[BaseException] = None
        for key in order:
            relay = self._relays.get(key)
            if relay is None:
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                result = await self._roundtrip(relay, method, path, headers, body,
                                               deadline)
            except RelayQuotaExceeded:
                raise                      # quota binds the account, not the route
            except asyncio.TimeoutError:
                last = MeshTunnelDown(
                    f"mesh_tunnel_down: {timeout}s budget exhausted mid-dial")
                with self._picker_lock:
                    self._picker.record_transport_failure(key, now_ms)
                break
            except MeshPeerError as e:
                last = e
                with self._picker_lock:
                    self._picker.record_transport_failure(key, now_ms)
                continue
            elapsed_ms = (time.monotonic() - started) * 1000.0
            with self._picker_lock:
                self._picker.record_task_latency(key, elapsed_ms, now_ms)
                self._picker.record_success(key, now_ms)
            return result
        raise MeshTunnelDown(
            f"mesh_tunnel_down: all {len(order)} relay route(s) failed for "
            f"{method} {path}: {last}")

    def _mint_cap(self, relay: RelayRoute) -> str:
        cap, _payload = self._relay_config.mint_capability_for(
            account_id=self._account_id, device_id=self._device_id,
            target_id=self.daemon_id, relay_id=relay.relay_id,
            region=relay.region, scopes=[DAEMON_DOOR_SCOPE])
        return cap

    async def _roundtrip(self, relay: RelayRoute, method: str, path: str,
                         headers: Mapping[str, str], body: bytes,
                         deadline: float) -> Tuple[int, Mapping[str, str], bytes]:
        remaining = deadline - asyncio.get_running_loop().time()
        connect = websockets.connect(
            relay.door_url(self._route(), self.daemon_id, self._mint_cap(relay)),
            max_size=FRAME_LIMIT * 2)
        try:
            ws = await asyncio.wait_for(connect, timeout=max(0.1, remaining))
        except websockets.InvalidStatus as e:
            # websockets >= 14 carries an http11.Response (.status_code);
            # older releases carried the parsed response with .status.
            response = e.response
            status = (getattr(response, "status_code", None)
                      or getattr(response, "status", None) or 0)
            if status == 429:
                raise RelayQuotaExceeded(
                    f"relay_quota: relay {relay.relay_id} refused the dial with "
                    f"429 (per-account stream quota, DR-3)") from e
            raise MeshTunnelDown(
                f"mesh_tunnel_down: relay {relay.relay_id} refused the upgrade "
                f"with HTTP {status}") from e
        except asyncio.TimeoutError as e:
            raise MeshTunnelDown(
                f"mesh_tunnel_down: relay {relay.relay_id} dial timed out") from e
        except OSError as e:
            raise MeshTunnelDown(
                f"mesh_tunnel_down: cannot reach relay {relay.relay_id} "
                f"at {relay.url}: {e}") from e
        try:
            return await self._exchange(ws, method, path, headers, body, deadline)
        finally:
            try:
                await asyncio.shield(ws.close())
            except Exception:
                pass                    # the tunnel is closing anyway

    def _route(self) -> str:
        """The door route name: the facade-prefix segment of the peer URL
        ('/livestack' -> 'livestack'). The relay matches it against its
        registered route pattern before routing by daemon_id."""
        return self._facade_prefix.strip("/")

    async def _exchange(self, ws, method: str, path: str,
                        headers: Mapping[str, str], body: bytes,
                        deadline: float) -> Tuple[int, Mapping[str, str], bytes]:
        """One ls-h1 roundtrip over the relay's caller door.

        The door is a RAW byte pipe, not the mux: connectClient wraps the
        caller's bytes into DATA frames for the target and strips the target's
        frames back to raw payloads (meshlink tunnels.ts) — the empty OPEN the
        mux would carry is target-side only. So the caller sends the request
        head and body as plain messages and reads the response envelope out of
        the reply stream until it completes; the relay closes the socket when
        the target settles the stream with CLOSE."""
        loop = asyncio.get_running_loop()

        async def _recv():
            return await asyncio.wait_for(ws.recv(),
                                          timeout=max(0.1, deadline - loop.time()))

        head = ls_h1.encode_request_head(method, path, dict(headers), len(body))
        await ws.send(head)
        for at in range(0, len(body), FRAME_LIMIT):
            await ws.send(body[at:at + FRAME_LIMIT])

        reader = ls_h1.ResponseReader()
        while True:
            try:
                message = await _recv()
            except asyncio.TimeoutError as e:
                raise MeshTunnelDown(
                    "mesh_tunnel_down: response did not complete before the "
                    "dial timeout") from e
            except websockets.ConnectionClosed as e:
                if reader.complete:
                    return reader.result()
                raise MeshTunnelDown(
                    "mesh_tunnel_down: relay closed the stream before the "
                    "response envelope completed") from e
            if isinstance(message, str):
                raise MeshTunnelDown(
                    "mesh_tunnel_down: unexpected text frame on the stream: "
                    f"{message[:80]!r}")
            try:
                reader.feed(message)
            except ls_h1.EnvelopeError as e:
                raise MeshTunnelDown(
                    f"mesh_tunnel_down: bad ls-h1 response envelope: {e}") from e
            if reader.complete:
                return reader.result()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Stop the dial loop. The broker never calls this (RestPeer has no
        close either and peers live for the process); tests and embedders do."""
        self._loop.close()
