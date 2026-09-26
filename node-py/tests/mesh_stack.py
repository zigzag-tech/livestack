"""Shared fixture for the meshlink integration tests (test_mesh_peer.py,
test_mixed_roster.py): the real relay engine + a real mesh_outbound_py
attachment + a loopback facade, with every private key held in Python.

Not named test_*, so pytest does not collect it. Every not-verifiable state
skips loudly (never passes silently), mirroring test_relay_control.py: no
meshlink checkout, no tsx, no built mesh_route_py .so, no relay dist.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from livestack_node import relay_control

REPO_ROOT = Path(__file__).resolve().parents[2]
MESHLINK_REPO = Path(os.environ.get("MESHLINK_REPO") or REPO_ROOT.parent / "meshlink")
MESHLINK_RELAY_SRC = MESHLINK_REPO / "packages" / "mesh_relay" / "src"
OUTBOUND_PKG = MESHLINK_REPO / "packages" / "mesh_outbound_py"

REALM = "livestack"
RELAY_ID = "relay-near"
ROUTE = "livestack"
# The livestack realm's cosmetics (DR-4), served by the real relay engine at
# the pinned meshlink build (MESHLINK.lock): the WS upgrade strips the realm's
# routePrefix before route matching, and the engine verifies door caps against
# the realm's configured typ/aud — so the harness serves the livestack prefix
# and caps wear livestack claims, exactly as production deploys them.
ROUTE_PREFIX = relay_control.DEFAULT_ROUTE_PREFIX
DOOR_PATH = relay_control.DEFAULT_DOOR_PATH
ACCOUNT_ID = "acct_livestack"
CALLER_ACCOUNT = "broker-acct"
CALLER_DEVICE = "broker-box"
CAP_TYPE = relay_control.DEFAULT_CAPABILITY_TYPE
CAP_AUDIENCE = relay_control.DEFAULT_CAPABILITY_AUDIENCE

# The compiled-in cosmetics of the relay package's DEFAULT (benchday) realm.
# A door cap wearing these against the livestack realm is the cross-realm
# spoof shape and must be refused (DR-4; asserted in test_mesh_peer.py).
BENCHDAY_CAP_TYPE = "benchday-speech-relay-capability"
BENCHDAY_CAP_AUDIENCE = "benchday-speech-relay"

# ---------------------------------------------------------------------------
# meshlink package availability (skip loudly, never pass silently)
# ---------------------------------------------------------------------------


def _require_checkout() -> None:
    if not (MESHLINK_RELAY_SRC / "server.ts").exists():
        pytest.skip(f"meshlink checkout not found at {MESHLINK_REPO}")
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    if not (OUTBOUND_PKG / "mesh_outbound_py" / "mux.py").exists():
        pytest.skip(f"mesh_outbound_py package not found at {OUTBOUND_PKG}")


def load_meshlink() -> None:
    """Put the meshlink Python packages where livestack_node.mesh_peer finds
    them: mesh_outbound_py on sys.path, mesh_route_py loaded from the
    cargo-built .so under rust/target. Idempotent across test modules."""
    _require_checkout()
    pkg = str(OUTBOUND_PKG)
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    if "mesh_outbound_py" not in sys.modules:
        candidates = sorted(
            MESHLINK_REPO.glob("rust/target/*/deps/libmesh_route_py.so"),
            key=lambda p: p.stat().st_mtime)
        if not candidates:
            pytest.skip("mesh_route_py .so not built — run `cargo build -p "
                        "mesh-route-py` in the meshlink checkout")
        spec = importlib.util.spec_from_file_location("mesh_route_py",
                                                      candidates[-1])
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            pytest.skip(f"mesh_route_py .so failed to import: {e}")
        sys.modules["mesh_route_py"] = module


# ---------------------------------------------------------------------------
# Minimal loopback node facade (the HTTP surface the target connector serves)
# ---------------------------------------------------------------------------


class Facade:
    """One node process's /livestack facade on loopback. Records every request
    so tests can assert the bytes arrived — the tunnel is the thing under
    test, the facade is just its destination."""

    def __init__(self, host_id: str, device_id: str, node_id: str,
                 units: list, port: int = 0):
        self.host_id = host_id
        self.device_id = device_id
        self.node_id = node_id
        self.units = units                     # [{"kind", "residency", ...}]
        self.resident = {}                     # kind -> bool
        self.requests = []                     # (method, path) in arrival order
        self.request_headers = []              # (method, path, headers) in order
        self.slow_seconds = 0.0                # /slow holds this long (quota test)
        self._lock = threading.Lock()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):     # keep the pytest capture clean
                pass

            def _answer(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append((method, self.path))
                    outer.request_headers.append(
                        (method, self.path, dict(self.headers)))
                if self.path.endswith("/slow"):
                    time.sleep(outer.slow_seconds)
                    payload = b'{"ok": true}'
                    self._send(200, payload)
                    return
                if method == "GET" and self.path.endswith("/residence"):
                    with outer._lock:
                        snap = {
                            "host_id": outer.host_id,
                            "device_id": outer.device_id,
                            "node_id": outer.node_id,
                            "busy": False,
                            "units": [
                                {**u, "resident": outer.resident.get(u["kind"], False)}
                                for i, u in enumerate(outer.units)
                            ],
                        }
                    self._send(200, json.dumps(snap).encode())
                    return
                if method == "GET" and self.path.endswith("/capability"):
                    self._send(200, json.dumps(
                        {"ready": True, "detail": "", "load": {"in_flight": 0,
                         "concurrency": 1}}).encode())
                    return
                if method == "POST" and self.path.endswith("/model/warm"):
                    req = json.loads(body or b"{}")
                    with outer._lock:
                        outer.resident[req.get("unit")] = True
                    self._send(200, b"{}")
                    return
                if method == "POST" and self.path.endswith("/model/evict"):
                    req = json.loads(body or b"{}")
                    with outer._lock:
                        outer.resident[req.get("unit")] = False
                    self._send(200, b"{}")
                    return
                if method == "POST" and self.path.endswith("/model/reclaim"):
                    self._send(200, b"{}")
                    return
                self._send(404, b'{"error": "not found"}')

            def _send(self, status: int, payload: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._answer("GET")

            def do_POST(self):
                self._answer("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name=f"facade-{device_id}", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/livestack"

    def saw(self, method: str, suffix: str) -> bool:
        with self._lock:
            return any(m == method and p.endswith(suffix)
                       for m, p in self.requests)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# The real-relay harness process (JSON lines over stdio)
# ---------------------------------------------------------------------------


class RelayHarness:
    def __init__(self, cap_keys: list):
        _require_checkout()
        tsx = (OUTBOUND_PKG / "node_modules" / ".bin" / "tsx")
        tsx = str(tsx) if tsx.exists() else shutil.which("tsx")
        if tsx is None:
            pytest.skip("tsx not found (npm install in mesh_outbound_py)")
        self._proc = subprocess.Popen(
            [tsx, str(Path(__file__).parent / "mesh_relay_harness.mjs"),
             json.dumps({
                 "realm": REALM, "relayId": RELAY_ID, "route": ROUTE,
                 "routePrefix": ROUTE_PREFIX, "doorPath": DOOR_PATH,
                 "hubPub": MeshKeys.hub_pub_b64(),
                 # Realm-tagged verify keys: the engine's multi-realm form
                 # (realmKeys/targetRealm/claimsForRealm) authorizes the door
                 # per realm, so a cap wears the livestack realm's cosmetics
                 # or is refused — mirroring meshlink's realm_door_e2e config.
                 "realmCapKeys": [
                     {"kid": k["kid"], "secret": k["secret"], "realm": REALM}
                     for k in cap_keys],
                 "attachmentAudience": relay_control.DEFAULT_ATTACHMENT_AUDIENCE,
                 "capabilityType": relay_control.DEFAULT_CAPABILITY_TYPE,
                 "capabilityAudience": relay_control.DEFAULT_CAPABILITY_AUDIENCE,
             })],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "MESHLINK_REPO": str(MESHLINK_REPO)})
        boot = self.cmd(None)
        if "ready" not in boot:
            raise RuntimeError(f"relay harness did not boot: {boot}")
        self.relay_url = boot["relayUrl"]
        self.port = boot["port"]

    def _read_line(self) -> dict:
        line = self._proc.stdout.readline()
        if not line:
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"relay harness exited "
                               f"(code {self._proc.returncode}): {err}")
        return json.loads(line)

    def cmd(self, obj) -> dict:
        if obj is not None:
            self._proc.stdin.write(json.dumps(obj) + "\n")
            self._proc.stdin.flush()
        return self._read_line()

    def set_quota(self, max_streams: int):
        assert self.cmd({"cmd": "set_quota",
                         "maxStreamsPerAccount": max_streams})["ok"]

    def relay_down(self):
        assert self.cmd({"cmd": "relay_down"})["ok"]

    def relay_up(self):
        assert self.cmd({"cmd": "relay_up"})["ok"]

    def stop(self):
        try:
            self.cmd({"cmd": "stop"})
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()


# ---------------------------------------------------------------------------
# Keys (Python holds all private material; the harness gets public keys only)
# ---------------------------------------------------------------------------


class MeshKeys:
    hub = Ed25519PrivateKey.generate()
    daemon = Ed25519PrivateKey.generate()
    cap_ring_keys = [{"kid": "k1", "secret": "secret-one", "active": True}]

    @classmethod
    def hub_pub_b64(cls) -> str:
        return base64.b64encode(cls.hub.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)).decode()

    @classmethod
    def daemon_pub_b64(cls) -> str:
        return base64.b64encode(cls.daemon.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)).decode()

    @classmethod
    def daemon_pem(cls) -> bytes:
        return cls.daemon.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())

    @classmethod
    def rotate_daemon(cls) -> None:
        """A new daemon key, SAME daemon_id — the DR-2 key-rotation shape."""
        cls.daemon = Ed25519PrivateKey.generate()


# ---------------------------------------------------------------------------
# Driving the target attachment (shared by both test modules)
# ---------------------------------------------------------------------------


def start_attach(loop, harness, facade, daemon_id, hub_key=None, daemon_key=None):
    """Attach the target side through the REAL mesh_outbound_py port, serving
    each accepted stream as one loopback HTTP request to the facade. `loop` is
    a running mesh_peer._LoopThread (the attachment is asyncio-native)."""
    from mesh_outbound_py.connectors.http_connector import HttpConnector
    from mesh_outbound_py.outbound import (OutboundOptions, RelayOffer,
                                           create_outbound_attachment,
                                           self_issued)

    hub_key = hub_key or MeshKeys.hub
    daemon_pem = (daemon_key or MeshKeys.daemon_pem())
    daemon_pub = base64.b64encode(
        (daemon_key or MeshKeys.daemon).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    token, _claims = relay_control.mint_attachment(
        hub_key, realm=REALM, daemon_id=daemon_id, daemon_key_b64=daemon_pub,
        relay_id=RELAY_ID, account_id=ACCOUNT_ID)
    base = facade.base_url[: -len("/livestack")]     # connector joins paths itself

    async def start():
        return create_outbound_attachment(OutboundOptions(
            offers=self_issued([RelayOffer(
                ws_url=f"{harness.relay_url}{DOOR_PATH}", token=token)]),
            signing_key_pem=daemon_pem,
            connector_factory=lambda: HttpConnector(base)))

    return loop.run(start(), 10)


def wait_attached(peer, limit_s: float = 20.0):
    """Poll refresh() until the tunnel answers — attach/restart has a backoff."""
    from livestack_node.mesh_peer import MeshPeerError

    deadline = time.monotonic() + limit_s
    while True:
        try:
            return peer.refresh()
        except MeshPeerError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def stop_attach(loop, att):
    """OutboundAttachment.stop schedules asyncio tasks — it must run ON its
    loop's thread, not the caller's."""

    async def _stop():
        att.stop()

    loop.run(_stop(), 10)
