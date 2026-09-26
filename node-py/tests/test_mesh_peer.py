"""MeshPeer against the REAL meshlink stack (openspec task 5.1).

The load-bearing property here is that both tunnel halves speak the same wire,
so nothing is imitated: the relay is the real `createRelayServer` package
hosted by tests/mesh_relay_harness.mjs (capability verify, quota 429 and route
matching all run in the relay's own code), the target is the real
`mesh_outbound_py` OutboundAttachment with its HTTP connector serving a
loopback facade, and the mux codec / ls-h1 envelope / route Picker are imported
from the pinned meshlink checkout (MESHLINK.lock, DR-5). Python holds every
private key; the harness receives public material only.

Named degradations (design.md "Failure semantics") are asserted as exception
classes with stable `degradation` names, and at the broker level as the
recorded probe error — a relay restart mid-membership must read as
`mesh_tunnel_down`, never as an empty snapshot.
"""
from __future__ import annotations

import threading
import time

import pytest

import mesh_stack
from mesh_stack import (CALLER_ACCOUNT, CALLER_DEVICE, CAP_AUDIENCE, CAP_TYPE,
                        Facade, MeshKeys, REALM, RELAY_ID, RelayHarness,
                        start_attach, stop_attach, wait_attached)

mesh_stack.load_meshlink()          # skips the whole module, loudly, if absent

from livestack_node import relay_control
from livestack_node.hostbroker import HostBroker
from livestack_node.mesh_peer import (MeshPeer, MeshTunnelDown,
                                      RelayQuotaExceeded, RelayRoute, _LoopThread)

GB = 1_000_000_000
DAEMON_ID = "gpu-box-7"
MESH_URL = f"mesh://{REALM}/{DAEMON_ID}/livestack"


def _units():
    return [{"kind": "qwen", "residency": 0, "busy": False,
             "footprint": {"vram_bytes": 9 * GB}}]


def _relay_config(harness):
    ring = relay_control.CapKeyRing(
        [relay_control.CapKey(k["kid"], k["secret"])
         for k in MeshKeys.cap_ring_keys], active_kid="k1")
    return relay_control.RelayConfig(
        urls=(harness.relay_url,), realm=REALM, cap_ring=ring,
        capability_type=CAP_TYPE, capability_audience=CAP_AUDIENCE)


def _make_peer(harness, url=MESH_URL, **kw):
    cfg = kw.pop("relay_config", None) or _relay_config(harness)
    return MeshPeer(url, relays=[RelayRoute(url=harness.relay_url,
                                            relay_id=RELAY_ID)],
                    relay_config=cfg, **kw)


@pytest.fixture(scope="module")
def stack():
    harness = RelayHarness(MeshKeys.cap_ring_keys)
    facade = Facade(host_id="host-mesh", device_id="dev-mesh-1",
                    node_id="loopback-node", units=_units())
    loop = _LoopThread()
    att = start_attach(loop, harness, facade, DAEMON_ID)
    peer = _make_peer(harness, account_id=CALLER_ACCOUNT,
                      device_id=CALLER_DEVICE, priorities={"qwen": 20},
                      fallback_footprints={"qwen": 9 * GB})
    try:
        wait_attached(peer)
        yield type("Stack", (), {"harness": harness, "facade": facade,
                                 "peer": peer, "loop": loop, "att": att})
    finally:
        stop_attach(loop, att)        # before the loop stops: stop schedules tasks
        peer.close()
        loop.close()
        facade.stop()
        harness.stop()


# ---------------------------------------------------------------------------
# Full request/response through an actual tunnel
# ---------------------------------------------------------------------------

def test_full_request_response_through_tunnel(stack):
    snap = stack.peer.refresh()
    assert snap["device_id"] == "dev-mesh-1"
    assert snap["host_id"] == "host-mesh"
    assert stack.facade.saw("GET", "/livestack/residence")

    units = stack.peer.units()
    assert set(units) == {"qwen"}
    # The broker-side priority override applies exactly as it does for RestPeer.
    assert units["qwen"].priority == 20

    stack.peer.warm("qwen")
    assert stack.facade.saw("POST", "/livestack/model/warm")
    # placements() reads the snapshot (RestPeer semantics: one read per cycle),
    # so refresh to see the resident flag the facade flipped.
    stack.peer.refresh()
    by_kind = {p.kind: p for p in stack.peer.placements()}
    assert by_kind["qwen"].device_id == "dev-mesh-1"

    stack.peer.evict("qwen")
    assert stack.facade.saw("POST", "/livestack/model/evict")


def test_capability_and_node_report(stack):
    cap = stack.peer.capability()
    assert cap["ready"] is True
    assert stack.peer.report() == {}               # the facade reports nothing extra
    # Identity is the URL's realm+daemon_id (DR-2), not the loopback self-report.
    assert stack.peer.node_id == f"mesh://{REALM}/{DAEMON_ID}"


def test_bearer_auth_rides_the_tunnel_unchanged(stack):
    # design.md: the tunnel adds ed25519/HMAC at the relay door; it does not
    # replace application auth. The facade must see the node's control token.
    peer = _make_peer(stack.harness, control_token="node-secret")
    try:
        peer.warm("qwen")
    finally:
        peer.close()
    authed = [h for m, p, h in stack.facade.request_headers
              if p.endswith("/model/warm")]
    assert any(h.get("Authorization") == "Bearer node-secret" for h in authed)


# ---------------------------------------------------------------------------
# Named degradations (jidoka — never hang, never silent)
# ---------------------------------------------------------------------------

def test_unattached_daemon_is_mesh_tunnel_down(stack):
    ghost = _make_peer(stack.harness, url=f"mesh://{REALM}/never-attached/livestack")
    try:
        with pytest.raises(MeshTunnelDown) as ei:
            ghost.refresh()
        assert ei.value.degradation == "mesh_tunnel_down"
        assert "mesh_tunnel_down" in str(ei.value)
    finally:
        ghost.close()


def test_relay_restart_mid_membership_is_named_not_silent(stack):
    peer = stack.peer
    broker = HostBroker(devices=None, peers=[peer],
                        device_config={"dev-mesh-1": {"vram_bytes": 24 * GB,
                                                      "reserved": 0}})
    key = peer.base
    assert broker.snapshot() is not None          # healthy: seed the memory
    assert key in broker._last_good

    stack.harness.relay_down()
    try:
        # The broker must NOT blind on the dead peer, and the failure it
        # recorded must carry the named degradation.
        assert broker.snapshot() is not None
        assert "mesh_tunnel_down" in broker._last_probe_error[key]
        # Unreachable is not empty: the remembered read is kept.
        assert broker._remembered_peer(key) is not None
        with pytest.raises(MeshTunnelDown):
            peer.refresh()
    finally:
        stack.harness.relay_up()

    # Recovery: the target's outbound backoff re-attaches and the tunnel
    # answers again — no broker restart, no re-register.
    wait_attached(peer)
    assert broker.snapshot() is not None
    assert key in broker._last_good


def test_relay_429_is_named_relay_quota(stack):
    stack.harness.set_quota(1)                    # one stream slot per account
    stack.facade.slow_seconds = 3.0
    errors = []

    def hold_stream():
        try:
            stack.peer._http(f"{stack.peer.base}/slow", timeout=30)
        except Exception as e:                    # noqa: BLE001 - recorded only
            errors.append(e)

    holder = threading.Thread(target=hold_stream, daemon=True)
    holder.start()
    deadline = time.monotonic() + 10
    while not stack.facade.saw("GET", "/livestack/slow"):
        assert time.monotonic() < deadline, "slow request never reached the facade"
        time.sleep(0.05)
    try:
        with pytest.raises(RelayQuotaExceeded) as ei:
            stack.peer.refresh()
        assert ei.value.degradation == "relay_quota"
        assert "relay_quota" in str(ei.value)
    finally:
        stack.harness.set_quota(0)
        stack.facade.slow_seconds = 0.0
        holder.join(timeout=10)
    assert not errors, f"the held stream broke: {errors}"


# ---------------------------------------------------------------------------
# DR-2: identity is realm+daemon_id — stable across key rotation
# ---------------------------------------------------------------------------

def test_key_rotation_keeps_broker_identity_stable(stack):
    peer = stack.peer
    broker = HostBroker(devices=None, peers=[peer],
                        device_config={"dev-mesh-1": {"vram_bytes": 24 * GB,
                                                      "reserved": 0}})
    broker.snapshot()
    expect = f"mesh://{REALM}/{DAEMON_ID}"
    assert broker._node_id_seen[peer.base] == expect

    # Rotate the daemon key: same daemon_id, new ed25519 key, fresh attachment.
    stop_attach(stack.loop, stack.att)
    MeshKeys.rotate_daemon()
    att = start_attach(stack.loop, stack.harness, stack.facade, DAEMON_ID)
    try:
        wait_attached(peer)
        broker.snapshot()
        assert broker._node_id_seen[peer.base] == expect, (
            "key rotation must not change who the broker thinks this peer is")
        assert peer.base not in broker.peer_alias, (
            "rotation created a second identity for one daemon")
    finally:
        stack.att = att                          # let the module fixture stop it
