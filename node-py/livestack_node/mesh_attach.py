"""mesh_attach.py — the node's mesh attach loop (openspec Phase 6).

The node half of the mesh join: after the local facade is serving, attach
outbound to the realm's relay(s) so brokers and callers can dial this node's
``mesh://<realm>/<daemon_id>/<prefix>`` URL through the tunnel. The attach
itself is the meshlink ``mesh_outbound_py`` port — imported, not reimplemented
(DRY): it owns the wire (challenge/response, mux, backoff 1 s → 60 s), and
this module owns only the livestack decisions around it:

* WHEN to attach (the env surface — see :func:`resolve_mesh_target`).
* WHAT the tunnel terminates on (the port's HTTP connector against loopback
  uvicorn — one stream is one loopback HTTP request).
* WHEN the ``bdrt1`` attachment is renewed. The relay enforces
  ``exp <= iat + 300`` (:data:`relay_control.MAX_ATTACHMENT_TTL_SECONDS`), so
  renewal is the steady state, not an edge case: the port renews on the OPEN
  socket when a fresh token is pushed for a URL it is already on, so a renewal
  never drops a live stream. We mint at the full 300 s and renew at
  :data:`DEFAULT_RENEWAL_INTERVAL_S` (300 − the 60 s lead
  :mod:`relay_control` already documents).
* HOW failure reads on the health surface. A failed attach NEVER boot-blocks
  and never stops the loopback facade: the supervisor retries with the same
  1 s → 60 s ladder the port uses, and :class:`MeshAttachState` names the
  failure distinctly from "no mesh configured" — absence and failure must not
  look alike (jidoka; design.md "Failure semantics").

Identity (DR-2): the daemon_id the operator assigned is the node. The ed25519
daemon key only proves possession at the relay's challenge; it may be rotated
or regenerated without changing who the fleet thinks this node is. An unset
daemon key is therefore generated ephemeral per boot and logged — identity
still rides on daemon_id alone.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

from . import relay_control
from .mesh_peer import _LoopThread

#: The relay's attach-door cosmetic, set at relay DEPLOY time in the realm
#: record and therefore part of the relay config, not a node knob. The node
#: must state it (a guessed door is a 404, and a 404 must not look like a dead
#: relay) — the default is the door the livestack realm's harness serves.
DEFAULT_DOOR_PATH = "/livestack-attach"

#: Renewal fires 60 s before the relay's 300 s attachment ceiling, leaving a
#: full 240 s of valid tunnel per cycle (relay_control documents the lead).
DEFAULT_RENEWAL_INTERVAL_S = float(
    relay_control.MAX_ATTACHMENT_TTL_SECONDS - relay_control.DEFAULT_RENEWAL_LEAD_SECONDS)

#: The supervisor's retry ladder when attachment CONSTRUCTION fails (bad key,
#: no usable relay, minting error). The port's own 1 s → 60 s backoff covers
#: dial failures inside a live attachment; these numbers mirror it so the two
#: halves read as one policy.
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0

#: The account_id claim attachments are minted under, unless overridden.
DEFAULT_ACCOUNT_ID = "livestack-node"

_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


# ---------------------------------------------------------------------------
# Env surface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeshTarget:
    """WHO this node is on the mesh: realm + daemon_id (DR-2)."""
    realm: str
    daemon_id: str

    @property
    def node_id(self) -> str:
        """The broker-visible identity: ``mesh://<realm>/<daemon_id>`` —
        exactly what MeshPeer.node_id derives from the roster URL, so a
        node's self-report and its peer record can never disagree."""
        return f"mesh://{self.realm}/{self.daemon_id}"

    def advertised_url_for(self, facade_prefix: str) -> str:
        """The URL announced to brokers: identity + the facade prefix."""
        return f"{self.node_id}{facade_prefix}"


def looks_like_daemon_id(value: str) -> bool:
    """Does ``value`` name a mesh daemon_id rather than a host?

    The rule, deliberately conservative: a daemon_id contains NO DOT and NO
    SCHEME SEPARATOR and matches the daemon-id charset the relay enforces
    (``relay_control._DAEMON_ID_RE``). Anything with a dot is a hostname or
    IP; anything with ``://`` is an explicit scheme. The ambiguity this does
    not resolve — a dotless bare hostname like ``localhost`` — is documented
    rather than special-cased: operators naming one should set
    ``LIVESTACK_MESH_DAEMON_ID`` (or ``LIVESTACK_MESH_ENABLED=0``) explicitly.
    """
    value = (value or "").strip()
    if "." in value or "://" in value:
        return False
    return bool(relay_control._DAEMON_ID_RE.match(value))


def _parse_enabled(raw: str) -> Optional[bool]:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(
        f"LIVESTACK_MESH_ENABLED={raw!r} is not one of "
        f"{'/'.join(_TRUE + _FALSE)} — refusing to guess whether this node "
        "is supposed to be on the mesh")


def resolve_mesh_target(env: Optional[Mapping[str, str]] = None) -> Optional[MeshTarget]:
    """Is this node mesh-attached, and as whom?

    Enabled when ``LIVESTACK_MESH_ENABLED`` says so (an invalid value RAISES —
    that is a configuration error an operator must see, and serve.attach
    turns the raise into a named health degradation, never a boot failure).
    Unset, it is scheme-driven: ``LIVESTACK_NODE_HOST`` that
    :func:`looks_like_daemon_id` names the mesh directly — that IS the
    operator stating the node's mesh name, and it needs no second flag.

    The daemon_id comes from ``LIVESTACK_MESH_DAEMON_ID`` (wins — it is
    unambiguous), then from a daemon-id-shaped ``LIVESTACK_NODE_HOST``. The
    realm is the relay realm (:data:`relay_control.DEFAULT_REALM`). Mesh
    enabled with no resolvable daemon_id raises: a node told to join the mesh
    without a name is a configuration error, not an anonymous peer.
    """
    env = os.environ if env is None else env
    enabled = _parse_enabled(env.get("LIVESTACK_MESH_ENABLED") or "")

    host = (env.get("LIVESTACK_NODE_HOST") or "").strip()
    host_is_id = bool(host) and looks_like_daemon_id(host)
    if enabled is None:
        enabled = host_is_id
    if not enabled:
        return None

    daemon_id = (env.get("LIVESTACK_MESH_DAEMON_ID") or "").strip()
    if not daemon_id and host_is_id:
        daemon_id = host
    if not daemon_id or not looks_like_daemon_id(daemon_id):
        raise ValueError(
            "mesh is enabled but no daemon_id is set — "
            "LIVESTACK_MESH_DAEMON_ID (or a daemon-id-shaped "
            "LIVESTACK_NODE_HOST: no dot, no '://') is required")
    realm = (env.get("LIVESTACK_RELAY_REALM") or "").strip() \
        or relay_control.DEFAULT_REALM
    return MeshTarget(realm=realm, daemon_id=daemon_id)


def daemon_key_pem_from_env(env: Optional[Mapping[str, str]] = None, *,
                            log: Callable[[str], None] = print
                            ) -> Tuple[bytes, str]:
    """The node's ed25519 daemon key (PKCS#8 PEM, raw-pub-b64) for answering
    the relay's challenge.

    ``LIVESTACK_MESH_DAEMON_KEY_FILE`` wins over ``LIVESTACK_MESH_DAEMON_KEY``
    for the same reason it does everywhere a credential has both forms (a
    credential readable out of ``systemctl show`` is shared by every process
    on the box), and the file carries the fleet's 0600 discipline
    (:func:`relay_control._read_secret_file`). Unset, the key is GENERATED
    per boot and the log says so: identity is daemon_id (DR-2), not the key,
    so an ephemeral key costs nothing the fleet can observe — silence about
    it would cost debuggability.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    env = os.environ if env is None else env
    key_pem = b""
    key_file = (env.get("LIVESTACK_MESH_DAEMON_KEY_FILE") or "").strip()
    inline_key = (env.get("LIVESTACK_MESH_DAEMON_KEY") or "").strip()
    if inline_key:
        key_pem = inline_key.replace("\\n", "\n").encode("utf-8")
    if key_file:
        key_pem = relay_control._read_secret_file(
            key_file, "LIVESTACK_MESH_DAEMON_KEY_FILE", log).encode("utf-8")
    if key_pem:
        key = relay_control.load_attachment_signing_key(key_pem.decode("utf-8"))
        pub = relay_control.raw_public_key_b64(key)
        return key_pem, pub
    log("[mesh-attach] no LIVESTACK_MESH_DAEMON_KEY(_FILE) — generating an "
        "ephemeral daemon key for this boot. Identity is daemon_id (DR-2), "
        "so this changes nothing the fleet can see; set a key file if the "
        "relay operator wants a stable door key.")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return pem, relay_control.raw_public_key_b64(key)


# ---------------------------------------------------------------------------
# Health state (the /health "mesh" subsystem)
# ---------------------------------------------------------------------------

class MeshAttachState:
    """Thread-safe holder for the mesh attach state the health surface reads.

    States name themselves: ``absent`` (no mesh configured — the default),
    ``attaching``, ``attached``, ``degraded``. A failed attach is ``degraded``
    WITH the error, the attempt count and a timestamp — it can never be
    mistaken for ``absent``, which is the whole point (jidoka: absence and
    failure must not look alike). Transitions come from the port's event sink
    and the supervisor; every mutation is under one lock, and :meth:`snapshot`
    returns a plain dict safe to merge into ``/health``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: dict = {"state": "absent"}

    # -- transitions ---------------------------------------------------------

    def mark_absent(self) -> None:
        with self._lock:
            self._snap = {"state": "absent"}

    def mark_attaching(self, *, daemon_id: str, relays: Sequence[str]) -> None:
        with self._lock:
            snap = dict(self._snap)
            snap.update({"state": "attaching", "daemon_id": daemon_id,
                         "relays": list(relays),
                         "since": snap.get("since") or time.time()})
            self._snap = snap

    def mark_attempt(self) -> None:
        with self._lock:
            snap = dict(self._snap)
            snap["attempts"] = int(snap.get("attempts") or 0) + 1
            self._snap = snap

    def mark_attached(self, urls: Sequence[str]) -> None:
        with self._lock:
            snap = dict(self._snap)
            snap.update({"state": "attached",
                         "tunnels": list(urls),
                         "since": snap.get("since") or time.time()})
            snap.pop("error", None)
            snap.pop("last_error", None)
            self._snap = snap

    def mark_detached(self, url: str, reason: str, remaining: Sequence[str]) -> None:
        with self._lock:
            snap = dict(self._snap)
            if remaining:
                snap.update({"state": "attached", "tunnels": list(remaining)})
            else:
                snap.update({"state": "degraded",
                             "error": f"mesh tunnel dropped ({reason})"})
            self._snap = snap

    def mark_failed(self, error: str) -> None:
        """Attach failed (construction or every tunnel down). The error is
        named in full — this is the stop-the-line signal, and "mesh attach
        failed" must be attributable without log archaeology."""
        with self._lock:
            snap = dict(self._snap)
            snap.update({"state": "degraded", "error": str(error)[:400],
                         "since": snap.get("since") or time.time()})
            self._snap = snap

    def mark_dial_error(self, url: str, error: BaseException) -> None:
        """The last dial failure, recorded WITHOUT a state change: the port's
        own backoff owns retrying, and the next detached/attached event owns
        the state. What this adds is the WHY — the port reports "never_ready",
        and "never_ready" plus ``Connection refused`` is a diagnosis while
        "never_ready" alone is a shrug."""
        with self._lock:
            snap = dict(self._snap)
            snap["last_error"] = f"{url}: {type(error).__name__}: {error}"[:300]
            self._snap = snap

    def mark_renewed(self) -> None:
        with self._lock:
            snap = dict(self._snap)
            snap["renewals"] = int(snap.get("renewals") or 0) + 1
            snap["last_renewal"] = time.time()
            self._snap = snap

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)


# ---------------------------------------------------------------------------
# Offers: mint bdrt1 attachments, push renewals on the open socket
# ---------------------------------------------------------------------------

class MeshOfferSource:
    """An ``mesh_outbound_py`` offer source that mints its own ``bdrt1``
    tokens via :mod:`relay_control` (the port's ``self_issued`` shape, with
    renewal added).

    ``subscribe`` mints the initial offer set and hands it to the attachment;
    :meth:`push` re-mints every offer and hands the set again — for a URL the
    attachment is already on, the port sends the fresh token ON the open
    socket (a renewal: no redial, no dropped streams). Both run on the
    attachment's event loop (the caller submits them via the loop thread),
    because the port applies offers with ``asyncio.create_task``.
    """

    def __init__(self, *, relay_config: relay_control.RelayConfig,
                 daemon_id: str, daemon_key_b64: str,
                 relay_ids: Mapping[str, str], door_path: str,
                 account_id: str):
        self._config = relay_config
        self._daemon_id = daemon_id
        self._daemon_key_b64 = daemon_key_b64
        self._account_id = account_id
        self._door_path = door_path
        self._relay_ids = dict(relay_ids)
        self._cb: Optional[Callable[[list], None]] = None
        self._offers: list = []

    @property
    def offers(self) -> list:
        """The current offer set (tokens included) — tests and diagnostics."""
        return list(self._offers)

    def _mint_offers(self) -> list:
        from mesh_outbound_py.outbound import RelayOffer

        offers = []
        for url, relay_id in self._relay_ids.items():
            token, _claims = self._config.mint_attachment_for(
                daemon_id=self._daemon_id, daemon_key_b64=self._daemon_key_b64,
                relay_id=relay_id, account_id=self._account_id)
            offers.append(RelayOffer(ws_url=f"{url}{self._door_path}", token=token))
        if not offers:
            raise ValueError(
                "mesh attach: no usable relay — every LIVESTACK_RELAY_URLS "
                "entry needs a relay_id in LIVESTACK_RELAY_IDS, and "
                "LIVESTACK_RELAY_KEY(_FILE) must hold the realm's attachment "
                "key (a token minted for a guessed relay_id is born refused)")
        return offers

    def subscribe(self, on_offers: Callable[[list], None]) -> Callable[[], None]:
        self._cb = on_offers
        self.push()
        return self._unsubscribe

    def push(self) -> None:
        self._offers = self._mint_offers()
        if self._cb is not None:
            self._cb(list(self._offers))

    def _unsubscribe(self) -> None:
        self._cb = None


# ---------------------------------------------------------------------------
# The attach loop
# ---------------------------------------------------------------------------

class MeshAttachHandle:
    """What :func:`start_mesh_attach` returns: the running attachment plus the
    threads that supervise and renew it. ``stop`` tears everything down; it is
    idempotent. ``renew_now`` forces a renewal cycle (tests)."""

    def __init__(self, *, loop: _LoopThread, owns_loop: bool,
                 source: MeshOfferSource, state: MeshAttachState,
                 renewal_interval_s: float, facade_base: str,
                 signing_key_pem: bytes, dial: Optional[Callable],
                 sleep: Optional[Callable], log: Callable[[str], None]):
        self._loop = loop
        self._owns_loop = owns_loop
        self._source = source
        self._state = state
        self._renewal_interval_s = renewal_interval_s
        self._facade_base = facade_base
        self._signing_key_pem = signing_key_pem
        # The port's test seams; None means "the port's own default" — these
        # are passed through only when set, so None never overrides a default.
        self._dial = dial
        self._sleep = sleep
        self._log = log
        self._attachment = None          # set by the supervisor thread
        self._stop = threading.Event()
        self._supervisor = threading.Thread(
            target=self._supervise, name="livestack-mesh-attach", daemon=True)
        self._renewer = threading.Thread(
            target=self._renew_loop, name="livestack-mesh-renewal", daemon=True)

    # -- internals -----------------------------------------------------------

    def _supervise(self) -> None:
        """Construct the attachment, retrying with the 1 s → 60 s ladder on
        failure. Once constructed the PORT owns dial retries (same ladder,
        reported through the event sink); a construction failure here is a
        config-class error (bad key, no usable relay) and the health state
        names it on every attempt."""
        backoff = BACKOFF_INITIAL_S
        while not self._stop.is_set():
            self._state.mark_attempt()
            try:
                attachment = self._loop.run(self._create(), timeout=30)
            except Exception as e:  # noqa: BLE001 - the whole point: never die
                self._state.mark_failed(f"mesh attach failed: {e}")
                self._log(f"[mesh-attach] attach attempt failed, will retry "
                          f"in {backoff:.0f}s: {e}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            self._attachment = attachment
            return

    async def _create(self):
        from mesh_outbound_py.outbound import (OutboundEventSink,
                                               OutboundOptions,
                                               create_outbound_attachment)
        from mesh_outbound_py.connectors.http_connector import HttpConnector

        handle = self

        def _attached(url: str) -> None:
            urls = (handle._attachment.attached_urls()
                    if handle._attachment is not None else [url])
            handle._state.mark_attached(urls)

        def _detached(url: str, reason: str) -> None:
            remaining = ([u for u in handle._attachment.attached_urls() if u != url]
                         if handle._attachment is not None else [])
            handle._state.mark_detached(url, reason, remaining)

        def _note(kind: str, _data: dict) -> None:
            if kind == "renewed":
                handle._state.mark_renewed()

        events = OutboundEventSink(attached=_attached, detached=_detached,
                                   note=_note)
        options = dict(offers=self._source.subscribe,
                       signing_key_pem=self._signing_key_pem,
                       connector_factory=lambda: HttpConnector(self._facade_base),
                       events=events)
        # Track dial failures into the health state. The port deliberately
        # swallows dial exceptions (its backoff owns retrying); recording
        # the WHY here turns the port's "never_ready" into a diagnosis.
        # Wrapped around the INJECTED dial too — a test's fake dial fails
        # exactly as really as the default one.
        from mesh_outbound_py.outbound import dial_websocket
        state = self._state
        dial = self._dial or dial_websocket

        async def _tracking_dial(url, headers):
            try:
                return await dial(url, headers)
            except Exception as e:
                state.mark_dial_error(url, e)
                raise

        options["dial"] = _tracking_dial
        if self._sleep is not None:
            options["sleep"] = self._sleep
        return create_outbound_attachment(OutboundOptions(**options))

    def _renew_loop(self) -> None:
        # Renewal is the steady state (attachments live ≤300 s): sleep first
        # — the initial token was just minted — then push fresh tokens until
        # stopped. A failed renewal is named on the health state; the still-
        # valid previous token keeps the tunnel up until the next cycle.
        while not self._stop.wait(self._renewal_interval_s):
            try:
                self.renew_now()
            except Exception as e:  # noqa: BLE001 - renewal must not kill the loop
                self._state.mark_failed(f"mesh attach renewal failed: {e}")
                self._log(f"[mesh-attach] renewal failed: {e}")

    def _renew_once(self) -> None:
        # The renewal COUNTER is the port's "renewed" note (a token actually
        # sent on an open socket) — counting here too would double-count, and
        # a same-second no-op renewal (byte-identical token) is not a renewal.
        self._source.push()

    # -- public surface --------------------------------------------------------

    @property
    def attachment(self):
        """The live ``OutboundAttachment`` once constructed (None while the
        supervisor is still retrying)."""
        return self._attachment

    @property
    def offer_source(self) -> MeshOfferSource:
        return self._source

    def wait_attached(self, timeout: float = 20.0) -> dict:
        """Block until the health state reads ``attached`` (or ``degraded``
        after a failure); returns the snapshot. Test seam — production code
        reads the health surface instead of blocking."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self._state.snapshot()
            if snap.get("state") in ("attached", "degraded"):
                return snap
            time.sleep(0.05)
        return self._state.snapshot()

    def renew_now(self) -> None:
        """Force one renewal cycle NOW (mint fresh tokens, push on the open
        sockets). Runs on the attachment's loop — ``push`` applies offers
        with ``asyncio.create_task`` and must therefore run where a loop is
        alive. Test seam and the renewer's body."""
        self._loop.run(self._renew_once_coro(), timeout=30)

    async def _renew_once_coro(self) -> None:
        self._renew_once()

    def stop(self) -> None:
        self._stop.set()
        if self._attachment is not None:
            attachment, self._attachment = self._attachment, None

            async def _stop_attachment():
                attachment.stop()

            try:
                self._loop.run(_stop_attachment(), timeout=15)
            except Exception as e:  # noqa: BLE001 - teardown best-effort
                self._log(f"[mesh-attach] teardown ignored: {e}")
        # Join before closing an OWNED loop: the supervisor may be blocked in
        # a construction attempt (bounded by loop.run's timeout), and closing
        # a loop out from under it raises "Cannot close a running event loop".
        self._supervisor.join(timeout=40)
        self._renewer.join(timeout=5)
        if self._owns_loop:
            self._loop.close()


def start_mesh_attach(facade_base_url: str, *, relay_config: relay_control.RelayConfig,
                      daemon_id: str, state: MeshAttachState,
                      relay_ids: Optional[Mapping[str, str]] = None,
                      account_id: str = DEFAULT_ACCOUNT_ID,
                      door_path: str = DEFAULT_DOOR_PATH,
                      signing_key_pem: Optional[bytes] = None,
                      renewal_interval_s: float = DEFAULT_RENEWAL_INTERVAL_S,
                      loop: Optional[_LoopThread] = None,
                      dial: Optional[Callable] = None,
                      sleep: Optional[Callable] = None,
                      log: Callable[[str], None] = print) -> MeshAttachHandle:
    """Start the mesh attach loop: outbound to the realm's relay(s), each
    accepted tunnel stream answered as one loopback HTTP request to
    ``facade_base_url`` (the connector joins facade paths itself, so the base
    is the origin only — ``http://127.0.0.1:<port>``, no prefix).

    ``relay_ids`` maps each configured relay URL to the relay's own id (the
    ``bdrt1`` claim is verified against it); unset, it is parsed from
    ``LIVESTACK_RELAY_IDS`` via :func:`relay_control.relay_ids_from_env`.
    ``signing_key_pem`` is the daemon key answering the relay's challenge —
    :func:`daemon_key_pem_from_env` is the production source. ``dial`` and
    ``sleep`` are the port's own test seams, passed straight through.

    Returns once the supervisor and renewer threads are RUNNING — the attach
    itself proceeds in the background and reports through ``state``; this
    function never blocks the caller's boot path and never raises for a
    network condition. (Construction args that are plainly misconfigured —
    no usable relay, no key — surface as ``degraded`` on the state, the
    supervisor retrying with backoff; only programming errors propagate.)
    """
    from mesh_outbound_py.outbound import OutboundOptions  # noqa: F401 - import check

    if not relay_config.urls:
        raise ValueError(
            "mesh attach requires at least one relay URL (LIVESTACK_RELAY_URLS)")
    if relay_config.attachment_key is None:
        raise ValueError(
            "mesh attach requires the realm's attachment key "
            "(LIVESTACK_RELAY_KEY(_FILE))")
    if relay_ids is None:
        relay_ids = relay_control.relay_ids_from_env(
            relay_config.urls, log=lambda m: log(f"[mesh-attach] {m}"))
    if signing_key_pem is None:
        signing_key_pem, daemon_key_b64 = daemon_key_pem_from_env(log=log)
    else:
        key = relay_control.load_attachment_signing_key(signing_key_pem.decode("utf-8"))
        daemon_key_b64 = relay_control.raw_public_key_b64(key)

    source = MeshOfferSource(relay_config=relay_config, daemon_id=daemon_id,
                             daemon_key_b64=daemon_key_b64, relay_ids=relay_ids,
                             door_path=door_path, account_id=account_id)
    state.mark_attaching(daemon_id=daemon_id,
                         relays=[f"{u}{door_path}" for u in relay_config.urls])

    owns_loop = loop is None
    loop = loop or _LoopThread()
    handle = MeshAttachHandle(loop=loop, owns_loop=owns_loop, source=source,
                              state=state, renewal_interval_s=renewal_interval_s,
                              facade_base=facade_base_url,
                              signing_key_pem=signing_key_pem, dial=dial,
                              sleep=sleep, log=log)
    handle._supervisor.start()
    handle._renewer.start()
    return handle
