"""Rotation drill (openspec task 7.1, second half): a LIVE tunnel through the
real relay harness survives a cap-key rotation; the verify window is proven at
the door itself, and the outgoing key retires only after its tokens are dead.

The ring's rotate()/retire() semantics (mint under the active kid, every key
in the ring still verifies, retire refuses the active kid) are unit-covered
against the relay's verify function in test_relay_control.py — this drill is
the end-to-end half those tests cannot reach:

  1. Boot the real relay harness with BOTH keys in its realm key set (the
     relay-side verify window) while the Python ring mints under k1 only.
  2. Drive a real tunnel (real mesh_outbound_py attachment + loopback facade)
     and hold a slow request in flight WHILE the ring rotates to k2 — the
     stream was authorized at connect, so it must settle normally.
  3. A caller capability minted under k1 BEFORE the rotation must still open
     the WS door AFTER it (the verify window, at the door — not via the
     verify function), while fresh mints wear k2 and verify.
  4. Once k1's tokens pass TTL + the relay's 30 s grace, the door refuses
     them (401); only then does the ring retire k1, after which k1 tokens
     fail client-side verify while k2 keeps serving.

The bdrt1 half of rotation — daemon key rotation must not change node_id
(DR-2) — is test_mesh_peer.py::test_key_rotation_keeps_broker_identity_stable;
it is deliberately not re-asserted here (DRY).

No sleeps stand in for the relay's clock: step 4 waits out a real TTL (30 s,
the relay's clamp floor) plus grace at the door, because expiry behavior at
the door is the property under test. Total drill time ≈ 70 s.
"""
from __future__ import annotations

import threading
import time

import pytest

import mesh_stack
from mesh_stack import (ACCOUNT_ID, CALLER_ACCOUNT, CALLER_DEVICE, CAP_AUDIENCE,
                        CAP_TYPE, DOOR_PATH, Facade, MeshKeys, REALM, RELAY_ID,
                        RelayHarness, ROUTE_PREFIX, start_attach, stop_attach,
                        wait_attached)

mesh_stack.load_meshlink()          # skips the whole module, loudly, if absent

from livestack_node import relay_control
from livestack_node.mesh_peer import (MeshPeer, MeshTunnelDown, RelayRoute,
                                      _LoopThread)

GB = 1_000_000_000
DAEMON_ID = "gpu-box-7"
MESH_URL = f"mesh://{REALM}/{DAEMON_ID}/livestack"

K1 = relay_control.CapKey(kid="k1", secret="secret-one")
K2 = relay_control.CapKey(kid="k2", secret="secret-two")
# The relay boots with BOTH keys in its realm key set — the verify window the
# rotation lives on. Which key mints is the Python ring's business only.
RELAY_KEY_SET = [{"kid": "k1", "secret": "secret-one", "active": True},
                 {"kid": "k2", "secret": "secret-two"}]


def _units():
    return [{"kind": "qwen", "residency": 0, "busy": False,
             "footprint": {"vram_bytes": 9 * GB}}]


def _config(harness, ring):
    return relay_control.RelayConfig(
        urls=(harness.relay_url,), realm=REALM, cap_ring=ring,
        capability_type=CAP_TYPE, capability_audience=CAP_AUDIENCE)


def _door_accepts(loop, harness, cap: str) -> bool:
    """Does this cap open the relay's WS door RIGHT NOW? The door answer is
    the load-bearing one: the real upgrade path, real verify, real clock."""
    import websockets

    route = RelayRoute(url=harness.relay_url, relay_id=RELAY_ID)
    url = route.door_url("livestack", DAEMON_ID, cap)

    async def _try():
        try:
            ws = await websockets.connect(url, open_timeout=10)
        except Exception:
            return False
        try:
            await ws.close()
        except Exception:
            pass
        return True

    return loop.run(_try(), 15)


@pytest.fixture(scope="module")
def stack():
    harness = RelayHarness(RELAY_KEY_SET)
    facade = Facade(host_id="host-rot", device_id="dev-rot-1",
                    node_id="loopback-node", units=_units())
    loop = _LoopThread()
    att = start_attach(loop, harness, facade, DAEMON_ID)
    ring_v1 = relay_control.CapKeyRing([K1], active_kid="k1")
    peer = MeshPeer(MESH_URL,
                    relays=[RelayRoute(url=harness.relay_url, relay_id=RELAY_ID)],
                    relay_config=_config(harness, ring_v1),
                    account_id=CALLER_ACCOUNT, device_id=CALLER_DEVICE,
                    priorities={"qwen": 20}, fallback_footprints={"qwen": 9 * GB})
    try:
        wait_attached(peer)
        yield type("Stack", (), {"harness": harness, "facade": facade,
                                 "peer": peer, "loop": loop, "att": att,
                                 "ring_v1": ring_v1})
    finally:
        stop_attach(loop, att)        # before the loop stops: stop schedules tasks
        peer.close()
        loop.close()
        facade.stop()
        harness.stop()


def test_cap_rotation_drill(stack):
    peer = stack.peer
    harness = stack.harness

    # Pre-rotation baseline: mints wear k1 and the tunnel answers.
    cap_a, _ = peer._relay_config.mint_capability_for(
        account_id=CALLER_ACCOUNT, device_id=CALLER_DEVICE,
        target_id=DAEMON_ID, relay_id=RELAY_ID, region="local",
        scopes=["terminal.proxy"])
    assert relay_control.peek_capability_kid(cap_a) == "k1"
    assert peer.refresh()["device_id"] == "dev-rot-1"

    # A k1 cap with the shortest legal TTL, minted BEFORE the rotation. It
    # must keep opening the door on the verify window and be refused only
    # after TTL + the relay's 30 s grace — the expiry half of the drill is
    # measured against ITS clock.
    short_a, _ = peer._relay_config.mint_capability_for(
        account_id=CALLER_ACCOUNT, device_id=CALLER_DEVICE,
        target_id=DAEMON_ID, relay_id=RELAY_ID, region="local",
        scopes=["terminal.proxy"], ttl_seconds=relay_control.MIN_TTL_SECONDS)
    short_a_exp = relay_control.MIN_TTL_SECONDS + 30   # + relay grace
    short_a_born = time.time()

    # (i) In-flight on the verify window: hold a slow request, rotate WHILE
    # it streams. The stream was authorized at connect; rotation must not
    # evict it, drop it, or force a reconnect.
    stack.facade.slow_seconds = 3.0
    settled = []

    def hold_slow():
        try:
            stack.peer._http(f"{stack.peer.base}/slow", timeout=30)
            settled.append("ok")
        except Exception as e:                    # noqa: BLE001 - asserted below
            settled.append(e)

    holder = threading.Thread(target=hold_slow, daemon=True)
    holder.start()
    deadline = time.monotonic() + 10
    while not stack.facade.saw("GET", "/livestack/slow"):
        assert time.monotonic() < deadline, "slow request never reached the facade"
        time.sleep(0.05)

    # THE ROTATION: k2 mints, k1 keeps verifying until its tokens die.
    ring_v2 = stack.ring_v1.rotate(K2)
    peer._relay_config = _config(harness, ring_v2)
    holder.join(timeout=15)
    stack.facade.slow_seconds = 0.0
    assert settled == ["ok"], f"in-flight stream broke across the rotation: {settled}"
    assert stack.peer.refresh()["device_id"] == "dev-rot-1", (
        "the same attachment must keep serving after the rotation — no "
        "eviction, no reconnect")

    # (ii) Verify window at the DOOR: the pre-minted k1 cap still opens it.
    assert _door_accepts(stack.loop, harness, short_a), (
        "a k1 cap minted before the rotation must still open the door on "
        "the verify window")
    # Fresh mints wear k2 and verify against the real relay.
    cap_b, _ = peer._relay_config.mint_capability_for(
        account_id=CALLER_ACCOUNT, device_id=CALLER_DEVICE,
        target_id=DAEMON_ID, relay_id=RELAY_ID, region="local",
        scopes=["terminal.proxy"])
    assert relay_control.peek_capability_kid(cap_b) == "k2"
    assert _door_accepts(stack.loop, harness, cap_b)
    assert peer.refresh()["device_id"] == "dev-rot-1"

    # (iii) Expiry, then retirement. Wait out k1's real TTL + grace at the
    # door: the relay is the enforcing clock and only its refusal is
    # evidence that k1 is dead. Refusal can only get MORE certain with
    # time, so a loaded CI box cannot flake this into a false pass.
    refuse_deadline = short_a_born + short_a_exp + 5
    while time.time() < refuse_deadline:
        time.sleep(1.0)
    assert not _door_accepts(stack.loop, harness, short_a), (
        "k1's cap must be refused at the door once TTL + grace have passed")
    assert peer.refresh()["device_id"] == "dev-rot-1", (
        "k2 dials must be unaffected by k1's expiry")

    # Only now is retirement clean: retire k1 and the ring verifies k2 but
    # no longer k1 (client-side mirror; the relay already proved it).
    ring_v3 = ring_v2.retire("k1")
    assert ring_v3.kids == ("k2",)
    ok, _, _ = relay_control.verify_capability(
        ring_v3, cap_b, target_id=DAEMON_ID, relay_id=RELAY_ID,
        scope="terminal.proxy")
    assert ok
    ok, _, reason = relay_control.verify_capability(
        ring_v3, short_a, target_id=DAEMON_ID, relay_id=RELAY_ID,
        scope="terminal.proxy")
    assert not ok and reason == "unknown_key_id"
    peer._relay_config = _config(harness, ring_v3)
    assert peer.refresh()["device_id"] == "dev-rot-1"
