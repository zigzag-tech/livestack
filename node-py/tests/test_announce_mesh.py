"""Phase 6 gate: the announce path over the mesh (openspec tasks 6.1/6.2).

The load-bearing property: a node with NO reachable IP — its facade listens on
loopback only — joins the fleet by attaching OUTBOUND to the relay, announcing
its ``mesh://`` name, and answering broker probes that arrive THROUGH the
tunnel. Everything here is real where it matters, reusing the Phase 5 harness
(tests/mesh_stack.py — DRY): the relay engine, the mesh_outbound_py
attachment, the MeshPeer dial. The attach loop under test is livestack's own
(mesh_attach), driven the way serve.attach drives it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import mesh_stack
from mesh_stack import (ACCOUNT_ID, CALLER_ACCOUNT, CALLER_DEVICE, CAP_AUDIENCE,
                        CAP_TYPE, DOOR_PATH, Facade, MeshKeys, REALM, RELAY_ID,
                        RelayHarness)

from livestack_node import mesh_attach, relay_control
from livestack_node.announce import facade_answers, register_once, start_registrar

GB = 1_000_000_000
DAEMON_ID = "gpu-box-7"
MESH_URL = f"mesh://{REALM}/{DAEMON_ID}/livestack"


def _units():
    return [{"kind": "qwen", "residency": 0, "busy": False,
             "footprint": {"vram_bytes": 9 * GB}}]


def _relay_config(harness):
    """The NODE side's config: the realm minting key signs the bdrt1."""
    return relay_control.RelayConfig(
        urls=(harness.relay_url,), realm=REALM, attachment_key=MeshKeys.hub)


def _caller_config(harness):
    """The BROKER side's config: cap ring wearing the door cosmetics the
    pinned relay build verifies (mesh_stack documents why)."""
    ring = relay_control.CapKeyRing(
        [relay_control.CapKey(k["kid"], k["secret"])
         for k in MeshKeys.cap_ring_keys], active_kid="k1")
    return relay_control.RelayConfig(
        urls=(harness.relay_url,), realm=REALM, cap_ring=ring,
        capability_type=CAP_TYPE, capability_audience=CAP_AUDIENCE)


def _start_node_attach(harness, facade, state, **kw):
    """The serve.attach call shape: attach outbound, terminate on loopback."""
    base = facade.base_url[: -len("/livestack")]     # connector joins paths itself
    return mesh_attach.start_mesh_attach(
        base, relay_config=_relay_config(harness), daemon_id=DAEMON_ID,
        relay_ids={harness.relay_url: RELAY_ID}, state=state,
        signing_key_pem=MeshKeys.daemon_pem(), account_id=ACCOUNT_ID,
        door_path=DOOR_PATH, renewal_interval_s=10 ** 6,
        log=lambda _m: None, **kw)


def _broker_peer(harness):
    from livestack_node.mesh_peer import MeshPeer, RelayRoute

    return MeshPeer(MESH_URL, relays=[RelayRoute(url=harness.relay_url,
                                                 relay_id=RELAY_ID)],
                    relay_config=_caller_config(harness),
                    account_id=CALLER_ACCOUNT, device_id=CALLER_DEVICE,
                    priorities={"qwen": 20}, fallback_footprints={"qwen": 9 * GB})


class _StubBroker:
    """Captures the announce payload — stands in for the broker's /peers."""

    def __init__(self):
        outer = self
        self.payloads = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.payloads.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"state":"fresh"}')

            def log_message(self, *_):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


def _wait_for(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _peek_attachment_exp(token: str) -> int:
    """The bdrt1 ``exp`` claim, unverified — diagnostics in tests only."""
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"]


# ---------------------------------------------------------------------------
# 6.1: the registrar announces the mesh name while probing loopback
# ---------------------------------------------------------------------------

def test_registrar_probes_loopback_but_announces_the_mesh_url():
    """Two addresses, one door: the self-probe must NEVER dial the mesh URL
    (a self-dial through the relay — see announce.facade_answers); it probes
    the loopback facade while the registered payload carries the mesh name."""
    facade = Facade(host_id="host-x", device_id="dev-x", node_id="n", units=[])
    probes, announced = [], []
    try:
        t = start_registrar(
            MESH_URL, host_id="host-x", kind="qwen", interval_s=0.05,
            probe_url=facade.base_url, log=lambda _m: None,
            answers=lambda u: probes.append(u) or facade_answers(u),
            register=lambda url, **_k: announced.append(url) or {},
        )
        try:
            assert _wait_for(lambda: len(announced) >= 1), "never announced"
        finally:
            t.stop()
        assert probes and all(u == facade.base_url for u in probes), \
            "the self-probe must dial loopback, never the mesh URL"
        assert announced == [MESH_URL]
    finally:
        facade.stop()


def test_register_once_carries_the_mesh_target_to_the_broker():
    """The announce payload IS the mesh URL — the one fact the broker cannot
    learn from the POST's source address."""
    broker = _StubBroker()
    try:
        register_once(MESH_URL, host_id="host-x", kind="qwen", broker=broker.url,
                      timeout=3.0)
        assert [p["facade_url"] for p in broker.payloads] == [MESH_URL]
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# The Phase 6 gate: no reachable IP, attaches outbound, probe through relay
# ---------------------------------------------------------------------------

def test_no_inbound_node_announces_mesh_and_broker_probes_green():
    mesh_stack.load_meshlink()          # skips loudly, never passes silently
    from mesh_stack import wait_attached as wait_peer

    harness = RelayHarness(MeshKeys.cap_ring_keys)
    facade = Facade(host_id="host-noip", device_id="dev-noip",
                    node_id="loopback-self-report", units=_units())
    state = mesh_attach.MeshAttachState()
    handle = _start_node_attach(harness, facade, state)
    peer = _broker_peer(harness)
    broker = _StubBroker()
    try:
        # The node has NO reachable IP: its only listener is loopback. The
        # assert is the premise of the test, stated so a harness change
        # cannot silently dissolve it.
        assert facade.server.server_address[0] == "127.0.0.1"
        assert "127.0.0.1" in facade.base_url

        # 1. Attach outbound through the real relay — the node side.
        snap = handle.wait_attached()
        assert snap["state"] == "attached", f"attach never came up: {snap}"

        # 2. The loopback self-probe is green — the condition that makes the
        #    mesh URL answerable (the tunnel terminates on this facade).
        assert facade_answers(facade.base_url) is True

        # 3. Register the mesh target with a broker; the announced address is
        #    the mesh name, not a loopback URL nobody off the box can dial.
        register_once(MESH_URL, host_id="host-noip", kind="qwen",
                      broker=broker.url, timeout=3.0)
        assert [p["facade_url"] for p in broker.payloads] == [MESH_URL]

        # 4. The broker's probe — a MeshPeer dialing /residence THROUGH the
        #    relay — is green.
        wait_peer(peer)
        snap = peer.refresh()
        assert snap["device_id"] == "dev-noip"
        assert facade.saw("GET", "/livestack/residence")
        # DR-2: the peer's identity is realm+daemon_id, not the facade's
        # loopback self-report.
        assert peer.node_id == f"mesh://{REALM}/{DAEMON_ID}"
    finally:
        peer.close()
        handle.stop()
        facade.stop()
        broker.close()
        harness.stop()


# ---------------------------------------------------------------------------
# 6.2: renewal before the 300 s ceiling, on the open socket
# ---------------------------------------------------------------------------

def test_renewal_pushes_a_fresh_token_before_expiry_without_dropping():
    mesh_stack.load_meshlink()
    from mesh_stack import wait_attached as wait_peer

    harness = RelayHarness(MeshKeys.cap_ring_keys)
    facade = Facade(host_id="host-ren", device_id="dev-ren", node_id="n",
                    units=_units())
    state = mesh_attach.MeshAttachState()
    handle = _start_node_attach(harness, facade, state)
    peer = _broker_peer(harness)
    try:
        assert handle.wait_attached()["state"] == "attached"

        # The steady-state renewal cadence renews BEFORE the relay's 300 s
        # attachment ceiling — renewal at 240 s of a 300 s token.
        assert mesh_attach.DEFAULT_RENEWAL_INTERVAL_S == 240.0
        assert mesh_attach.DEFAULT_RENEWAL_INTERVAL_S < \
            relay_control.MAX_ATTACHMENT_TTL_SECONDS

        before = handle.offer_source.offers
        assert len(before) == 1
        exp_before = _peek_attachment_exp(before[0].token)
        # iat/exp are second-granularity and ed25519 is deterministic, so a
        # same-second renewal mints a byte-IDENTICAL token (harmless: the port
        # treats it as a no-op and the still-valid token stays live). Advance
        # the clock one second to observe the fresh token a real 240 s cycle
        # would carry.
        time.sleep(1.1)

        # Drive one renewal cycle (what the renewer thread fires every 240 s).
        handle.renew_now()

        after = handle.offer_source.offers
        assert len(after) == 1
        assert after[0].ws_url == before[0].ws_url       # same relay, same door
        assert after[0].token != before[0].token          # a NEW token
        assert _peek_attachment_exp(after[0].token) > exp_before
        snap = state.snapshot()
        assert snap["renewals"] >= 1
        assert snap["state"] == "attached"

        # Renewal rides the OPEN socket: the tunnel never dropped.
        assert handle.attachment.attached_urls(), "renewal re-dialed — streams would have died"
        wait_peer(peer)
        peer.refresh()
        assert facade.saw("GET", "/livestack/residence")
    finally:
        peer.close()
        handle.stop()
        facade.stop()
        harness.stop()


# ---------------------------------------------------------------------------
# 6.2: attach failure — named degradation, loopback keeps serving, backoff
# ---------------------------------------------------------------------------

def test_attach_failure_reports_unhealthy_and_retries_with_backoff():
    """Relay unreachable: the health surface names the mesh-attach failure
    distinctly from 'no mesh configured', the facade still serves loopback,
    and the retry ladder is the port's own 1 s → 60 s (driven here through
    the port's injectable sleep — no real waiting)."""
    mesh_stack.load_meshlink()

    sleeps = []

    async def _refused_dial(_url, _headers):
        raise ConnectionRefusedError("no relay here")

    async def _fast_sleep(seconds):
        sleeps.append(seconds)          # record the backoff, don't serve it
        await asyncio.sleep(0)          # but DO yield: a sleep that never
        # yields would spin the port's retry loop without ever letting its
        # event loop breathe, and teardown could never land.

    state = mesh_attach.MeshAttachState()
    cfg = relay_control.RelayConfig(
        urls=("ws://127.0.0.1:9",), realm=REALM, attachment_key=MeshKeys.hub)
    handle = mesh_attach.start_mesh_attach(
        "http://127.0.0.1:1", relay_config=cfg, daemon_id=DAEMON_ID,
        relay_ids={"ws://127.0.0.1:9": RELAY_ID}, state=state,
        signing_key_pem=MeshKeys.daemon_pem(), account_id=ACCOUNT_ID,
        door_path=DOOR_PATH, renewal_interval_s=10 ** 6,
        dial=_refused_dial, sleep=_fast_sleep, log=lambda _m: None)
    try:
        assert _wait_for(lambda: len(sleeps) >= 12, timeout=10), \
            f"attach never retried: {state.snapshot()}"
        snap = state.snapshot()
        assert snap["state"] == "degraded"
        assert "never_ready" in snap["error"] or "attach" in snap["error"]
        assert "ConnectionRefusedError" in (snap.get("last_error") or "")
        assert snap.get("attempts", 0) >= 1
        # The port's backoff ladder, observed: it doubles the previous delay
        # BEFORE sleeping (initial_ms is the ladder's floor, so the first
        # observed wait is already 2 s), capping at 60 s:
        # 2 → 4 → 8 → 16 → 32 → 60 → 60 …
        assert sleeps[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
        assert sleeps[5] == 60.0
        assert all(s == 60.0 for s in sleeps[5:])
    finally:
        handle.stop()


def test_health_surface_names_mesh_failure_distinctly_from_absent():
    """The serve.attach integration: /health carries a 'mesh' subsystem.
    Not configured → 'absent' and status 'ok'; relay unreachable → 'degraded'
    with the failure named, the loopback facade still serving, node_id the
    stable daemon identity (DR-2). Never boot-blocks."""
    mesh_stack.load_meshlink()          # the serve path attaches for real
    pytest.importorskip("livestack_node")
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from cryptography.hazmat.primitives import serialization

    from fastapi import FastAPI
    from livestack_node import (ManagedUnit, ResidencyPolicy, attach, noop_free)

    def units():
        return {"qwen": ManagedUnit("qwen", loader=lambda: "m", freer=noop_free,
                                    residency_policy=ResidencyPolicy.UNPINNED)}

    async def _get(app, path):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test") as c:
            return await c.get(path)

    # -- mesh NOT configured: absent, healthy, status ok -------------------
    app_plain = FastAPI()
    attach(app_plain, host_id="h", kind="qwen", units=units(), idle_seconds=120,
           coload=True, gpu_call=lambda fn: fn(), port=8899)

    async def _absent():
        h = (await _get(app_plain, "/livestack/health")).json()
        assert h["status"] == "ok"
        assert h["mesh"]["state"] == "absent"
        cap = (await _get(app_plain, "/livestack/capability")).json()
        assert cap["node_id"].endswith(":8899"), "plain node keeps hostname:port"

    import asyncio
    asyncio.run(_absent())

    # -- mesh configured, relay dead: degraded, named, loopback serving ------
    hub_pem = MeshKeys.hub.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()

    import os
    prev = {k: os.environ.get(k) for k in (
        "LIVESTACK_MESH_ENABLED", "LIVESTACK_MESH_DAEMON_ID",
        "LIVESTACK_RELAY_URLS", "LIVESTACK_RELAY_IDS", "LIVESTACK_RELAY_KEY",
        "LIVESTACK_REGISTER")}
    os.environ.update({
        "LIVESTACK_MESH_ENABLED": "1",
        "LIVESTACK_MESH_DAEMON_ID": DAEMON_ID,
        "LIVESTACK_RELAY_URLS": "ws://127.0.0.1:9",
        "LIVESTACK_RELAY_IDS": json.dumps({"ws://127.0.0.1:9": RELAY_ID}),
        "LIVESTACK_RELAY_KEY": hub_pem,
        "LIVESTACK_REGISTER": "0",      # the registrar is covered above
    })
    try:
        app_mesh = FastAPI()
        attach(app_mesh, host_id="h", kind="qwen", units=units(),
               idle_seconds=120, coload=True, gpu_call=lambda fn: fn(),
               port=8899)

        async def _degraded():
            # Poll: the attach supervisor retries on its own clock.
            for _ in range(200):
                h = (await _get(app_mesh, "/livestack/health")).json()
                if h["mesh"]["state"] == "degraded":
                    break
                await asyncio.sleep(0.05)
            assert h["status"] == "degraded"
            mesh = h["mesh"]
            assert mesh["state"] == "degraded"
            assert mesh["error"], "failure must be named, not silent"
            # Absence and failure do not look alike: this is NOT the absent
            # state, and the error names the mesh attach, not the facade.
            assert "degraded" != "absent"
            assert "never_ready" in mesh["error"] or "attach" in mesh["error"]
            # The loopback facade still serves — no boot-block.
            r = await _get(app_mesh, "/livestack/residence")
            assert r.status_code == 200
            cap = (await _get(app_mesh, "/livestack/capability")).json()
            assert cap["node_id"] == f"mesh://{REALM}/{DAEMON_ID}"
            return mesh

        mesh = asyncio.run(_degraded())
        assert "refused" in (mesh.get("last_error") or "").lower() or \
               "refused" in mesh["error"].lower()
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
