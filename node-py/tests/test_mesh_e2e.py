"""The consolidated no-inbound e2e lane (openspec task 9.1).

One test process, the FULL journey, everything real where it matters — this
lane CONSOLIDATES what the per-phase tests proved separately (DRY: it reuses
tests/mesh_stack.py + tests/mesh_relay_harness.mjs and drives the same real
components, asserting the joins BETWEEN the phases rather than re-drilling
each one):

  (0) A node environment with NO reachable IP. A real network namespace
      (`unshare -n`) is not available unprivileged in CI (verified:
      `unshare -n` fails with EPERM for a non-root uid on this host), so
      no-inbound is SIMULATED: every listener is loopback-only and the lane
      asserts the node's own config — resolved through the production env
      surface (`resolve_mesh_target` / `RelayConfig.from_env`) — contains no
      dialable non-loopback address. The simulation is stated here so a
      future unprivileged-userns CI can tighten it.
  (1) Outbound attach (the real mesh_outbound_py port, loopback HTTP
      connector) — the node dials OUT; nothing dials in.
  (2) Register the mesh:// target with the broker through the real
      register_url path (what POST /peers serves), with the broker-side
      MeshPeer built by the real scheme-selecting make_peer from the
      production relay env — the Phase 5 dispatch, exercised end to end.
  (3) The broker's /residence probe through the tunnel is green.
  (4) POST /fleet/admit places the work on the mesh node (real build_app
      route over the real broker's fleet view).
  (5) warm/evict roundtrip: the host broker dispatches both THROUGH the
      tunnel under one journey owner.
  (6) Relay restart: suspect rung, placements intact, zero evictions,
      self-recovery (consolidates test_mixed_roster.py's drill).
  (7) Cap-key rotation k1→k2 mid-flight: the stream settles, fresh mints
      verify, broker identity stable (the TTL+grace expiry half stays in
      test_relay_rotation.py; the daemon-key/bdrt1 half stays in
      test_mesh_peer.py — both deliberately NOT re-run here).
  (8) Quota exhaustion: the relay's 429 surfaces as the NAMED degradation
      relay_quota on the membership row — distinct from mesh_tunnel_down and
      from node unhealth (the loopback facade answers throughout).

Task 9.1's ledger obligation: the lane asserts the ledger JOINS — the fleet
admit record, its hosted lease, and the host-level grant/load/evict records
must reconcile in one query over the journey owner.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

import pytest

import mesh_stack
from mesh_stack import (ACCOUNT_ID, DOOR_PATH, Facade, MeshKeys, REALM,
                        RELAY_ID, RelayHarness)

mesh_stack.load_meshlink()          # skips the whole module, loudly, if absent

from cryptography.hazmat.primitives import serialization  # noqa: E402

from livestack_node import mesh_attach, relay_control  # noqa: E402
from livestack_node.announce import facade_answers  # noqa: E402
from livestack_node.hostbroker import HostBroker, peer_key  # noqa: E402
from livestack_node.hostd import (DEFAULT_FOOTPRINTS, build_app,  # noqa: E402
                                  make_peer)
from livestack_node.ledger import JsonlLedger, validate  # noqa: E402
from livestack_node.mesh_peer import MeshPeer, RelayQuotaExceeded  # noqa: E402
from livestack_node.planner import Request  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

GB = 1_000_000_000
DAEMON_ID = "gpu-box-e2e"
MESH_URL = f"mesh://{REALM}/{DAEMON_ID}/livestack"
NODE_ID = f"mesh://{REALM}/{DAEMON_ID}"
JOURNEY_OWNER = "e2e-journey"
DEVICE = "dev-e2e-1"

# The relay boots with BOTH keys in its realm key set — the verify window the
# rotation (step 7) lives on, same discipline as test_relay_rotation.py. Only
# the caller ring's ACTIVE kid decides what fresh mints wear.
RELAY_KEY_SET = [{"kid": "k1", "secret": "secret-one", "active": True},
                 {"kid": "k2", "secret": "secret-two", "active": False}]


def _units():
    return [{"kind": "qwen", "residency": 2, "busy": False,
             "footprint": {"vram_bytes": 9 * GB}},
            # The pressure unit: 20 GB does not fit beside qwen on a 24 GB
            # card, and it outranks qwen (explicit priority below), so
            # admitting it (step 5) lawfully preempts qwen. Residency 2 is
            # UNPINNED (planner.Residency: HARD_PIN=0, SOFT_PIN=1, UNPINNED=2)
            # — a HARD_PINNED resident is never a preemption victim.
            {"kind": "llm", "residency": 2, "busy": False,
             "footprint": {"vram_bytes": 20 * GB}}]


# Priority ints: LOWER is more important (hostd.DEFAULT_PRIORITIES follows the
# same convention). llm outranks qwen so the step-5 admission can preempt it.
JOURNEY_PRIORITIES = {"qwen": 30, "llm": 10}


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _assert_loopback_only(urls, what: str):
    """No dialable non-loopback address in `urls` — the no-inbound premise,
    asserted so a harness change cannot silently dissolve it."""
    for url in urls:
        host = urlparse(url if "://" in url else f"//{url}").hostname or ""
        assert host in ("127.0.0.1", "localhost", "::1"), \
            f"{what} names a dialable non-loopback address: {url!r}"


def _wait_for(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    return pred()


@pytest.fixture(scope="module")
def journey(tmp_path_factory):
    harness = RelayHarness(RELAY_KEY_SET)
    facade = Facade(host_id="host-e2e", device_id=DEVICE,
                    node_id="loopback-self-report", units=_units())

    # -- the node environment, through the PRODUCTION env surface -----------
    node_env = {
        "LIVESTACK_MESH_ENABLED": "1",
        # The operator states the mesh name AS the node host (Phase 6):
        # daemon-id-shaped, so it resolves directly to the mesh target.
        "LIVESTACK_NODE_HOST": DAEMON_ID,
        "LIVESTACK_MESH_DAEMON_ID": DAEMON_ID,
        "LIVESTACK_RELAY_URLS": harness.relay_url,
        "LIVESTACK_RELAY_IDS": json.dumps({harness.relay_url: RELAY_ID}),
        "LIVESTACK_RELAY_KEY": _pem(MeshKeys.hub),
        "LIVESTACK_MESH_DAEMON_KEY": _pem(MeshKeys.daemon),
    }
    target = mesh_attach.resolve_mesh_target(node_env)
    assert target is not None and target.daemon_id == DAEMON_ID
    node_cfg = relay_control.RelayConfig.from_env(node_env)
    assert node_cfg.configured, "node env must resolve to an attachable config"
    signing_pem, _pub = mesh_attach.daemon_key_pem_from_env(
        node_env, log=lambda _m: None)
    # Step (0): no reachable IP. Every address the node's config could cause
    # anything to LISTEN on or DIAL out from is loopback here; the mesh join
    # is what makes the node reachable at all.
    _assert_loopback_only(node_cfg.urls, "node relay config")
    _assert_loopback_only([facade.base_url], "node facade")
    assert facade.server.server_address[0] == "127.0.0.1"

    state = mesh_attach.MeshAttachState()
    handle = mesh_attach.start_mesh_attach(
        facade.base_url[: -len("/livestack")],
        relay_config=node_cfg, daemon_id=target.daemon_id,
        relay_ids={harness.relay_url: RELAY_ID}, state=state,
        signing_key_pem=signing_pem, account_id=ACCOUNT_ID,
        door_path=DOOR_PATH, renewal_interval_s=10 ** 6,
        log=lambda _m: None)

    # -- the broker side -----------------------------------------------------
    ledger_path = tmp_path_factory.mktemp("e2e") / "journey.jsonl"
    # An offset clock: real time PLUS a jumpable offset. Step 5 jumps the
    # offset past the planner's anti-thrash residency floor (min_residency_s)
    # so the llm admit may preempt the just-warmed qwen — deterministically,
    # with no real waiting. Everything else (roster probe cadence, mia aging)
    # rides real time exactly as in production; a frozen clock would freeze
    # the event-demoted peer's re-probe cadence with it.
    offset = [0.0]
    broker = HostBroker(devices=None, peers=[],
                        device_config={DEVICE: {"vram_bytes": 24 * GB,
                                                "reserved": 0}},
                        mesh_suspect_probe_s=0.5,
                        ledger=JsonlLedger(str(ledger_path)),
                        clock=lambda: time.time() + offset[0])
    # The broker-side (caller) relay surface, read by the REAL make_peer
    # through RelayConfig.from_env when the mesh URL is registered (step 2) —
    # the same env a fleet broker operator sets.
    caller_env = {
        "LIVESTACK_RELAY_URLS": harness.relay_url,
        "LIVESTACK_RELAY_IDS": json.dumps({harness.relay_url: RELAY_ID}),
        "LIVESTACK_RELAY_CAP_KEYS": json.dumps(RELAY_KEY_SET),
    }
    prev = {k: os.environ.get(k) for k in caller_env}
    os.environ.update(caller_env)
    prev_replan = os.environ.get("LIVESTACK_REPLAN_INTERVAL")
    os.environ["LIVESTACK_REPLAN_INTERVAL"] = "0"
    app = build_app(broker)
    try:
        yield type("Journey", (), {
            "harness": harness, "facade": facade, "handle": handle,
            "broker": broker, "ledger": broker.ledger, "app": app,
            "offset": offset})
    finally:
        handle.stop()
        for p in broker.peers:
            p.close()
        facade.stop()
        harness.stop()
        for k, v in {**caller_env, "LIVESTACK_REPLAN_INTERVAL": prev_replan}.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _records(ledger):
    records = ledger.read()
    assert all(not validate(r) for r in records)
    return records


def test_mesh_e2e_no_inbound_full_journey(journey):
    broker, facade, harness = journey.broker, journey.facade, journey.harness

    # -- (1) outbound attach: the node dials OUT, nothing dials in ----------
    snap = journey.handle.wait_attached()
    assert snap["state"] == "attached", f"attach never came up: {snap}"

    # -- (2) register the mesh:// target with the broker through the real ---
    # -- register path (what POST /peers serves); the MeshPeer is built by  --
    # -- the real scheme-selecting make_peer from the caller relay env.     --
    broker.register_url(
        MESH_URL,
        make_peer=lambda u: make_peer(u, priorities=JOURNEY_PRIORITIES,
                                      fallback_footprints=DEFAULT_FOOTPRINTS),
        host_id="host-e2e", device_id=DEVICE, kinds=["qwen", "llm"])
    assert len(broker.peers) == 1
    peer = broker.peers[0]
    assert isinstance(peer, MeshPeer)
    assert peer_key(peer) == MESH_URL          # opaque URL-keyed record (5.2)

    # -- (3) the broker's /residence probe through the tunnel is green ------
    assert _wait_for(lambda: _probe_ok(peer))
    assert peer.refresh()["device_id"] == DEVICE
    assert facade.saw("GET", "/livestack/residence")
    # DR-2: identity is realm+daemon_id, not the loopback self-report.
    assert peer.node_id == NODE_ID
    # The reconcile tick that fills the fleet view (peer_units/kinds) — the
    # same snapshot /fleet/admit's fleet_view reads.
    broker.snapshot()

    # -- (4) /fleet/admit places the work on the mesh node ------------------
    # The real route through ASGI (TestClient is unavailable in this env —
    # test_fleet_admit_regions fails pre-existing on the same ambient
    # httpx/starlette; ASGITransport is the compatible driver and runs the
    # same build_app app).
    import asyncio

    import httpx

    async def _admit(payload):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=journey.app),
                base_url="http://test") as c:
            return await c.post("/fleet/admit", json=payload)

    r = asyncio.run(_admit({"kind": "qwen", "owner": JOURNEY_OWNER}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["granted"] is True, body.get("reason")
    assert body["target"]["host_id"] == "host-e2e"
    assert body["target"]["device_id"] == DEVICE
    assert NODE_ID in (body["target"].get("target_id") or "")
    lease_id = body["lease_id"]
    assert lease_id, "a grant must book its lease (capacity is the product)"
    # A live caller heartbeats its lease; the journey is the caller here.
    assert broker.hosted_heartbeat(lease_id)

    # -- (5) warm/evict roundtrip, dispatched THROUGH the tunnel ------------
    assert broker.admit(Request("job-e2e-1", "qwen", owner=JOURNEY_OWNER,
                                created_at=time.time() + journey.offset[0])) == DEVICE
    assert facade.saw("POST", "/livestack/model/warm")
    assert facade.resident.get("qwen") is True
    # Age past the anti-thrash residency floor so preemption is legal.
    journey.offset[0] += 60.0
    assert broker.admit(Request("job-e2e-2", "llm", owner=JOURNEY_OWNER,
                                created_at=time.time() + journey.offset[0])) == DEVICE
    # 20 GB does not fit beside qwen on a 24 GB card: qwen steps aside.
    assert facade.saw("POST", "/livestack/model/evict")
    assert facade.resident.get("qwen") is False
    assert facade.resident.get("llm") is True
    # One reconcile tick so the broker's last good read carries llm's
    # placement — the read the remembered-placement path feeds the world
    # from during the outage below (a production broker ticks continuously).
    broker.snapshot()
    assert any(p.kind == "llm" and p.device_id == DEVICE
               for p in broker.snapshot().placements)

    # -- (6) relay restart: suspect, placements intact, zero evictions ------
    # The demotion happens ON the probe failure inside snapshot(), and only a
    # snapshot that actually probed (and failed) feeds the remembered
    # placements — one taken inside the fast re-probe backoff skips the peer
    # entirely. So every wait below captures the world from the SAME snapshot
    # that observed the state it asserts.
    def _world_with_llm():
        w = broker.snapshot()
        return w if any(p.kind == "llm" and p.device_id == DEVICE
                        for p in w.placements) else None

    harness.relay_down()
    try:
        world = None

        def _demoted():
            nonlocal world
            world = broker.snapshot()
            return broker.roster.state_of(MESH_URL) == "suspect"
        assert _wait_for(_demoted)
        row = {x["peer"]: x for x in broker.membership_snapshot()}[MESH_URL]
        assert row["state"] == "suspect"
        assert row.get("degradation") == "mesh_tunnel_down"
        # The remembered read keeps llm's placement in the world across the
        # episode — a relay restart never evicts.
        assert broker._remembered_peer(MESH_URL) is not None
        assert any(p.kind == "llm" and p.device_id == DEVICE
                   for p in world.placements)
        time.sleep(0.7)                    # one fast re-probe window
        assert _wait_for(_world_with_llm) is not None, \
            "remembered placement lost mid-episode"
        assert broker.roster.state_of(MESH_URL) == "suspect"
    finally:
        harness.relay_up()
    assert _wait_for(lambda: _probe_ok(peer))
    world = None

    def _recovered():
        nonlocal world
        world = broker.snapshot()
        return broker.roster.state_of(MESH_URL) == "fresh"
    assert _wait_for(_recovered)
    assert any(p.kind == "llm" and p.device_id == DEVICE
               for p in world.placements)
    evicts = [m for m, p in facade.requests
              if m == "POST" and p.endswith("/model/evict")]
    assert len(evicts) == 1, "the relay restart must not evict anything"

    # -- (7) cap-key rotation k1→k2: in-flight settles, identity stable -----
    facade.slow_seconds = 3.0
    settled = []

    def hold_slow():
        try:
            peer._http(f"{peer.base}/slow", timeout=30)
            settled.append("ok")
        except Exception as e:                    # noqa: BLE001 - asserted below
            settled.append(e)

    holder = threading.Thread(target=hold_slow, daemon=True)
    holder.start()
    deadline = time.monotonic() + 10
    while not facade.saw("GET", "/livestack/slow"):
        assert time.monotonic() < deadline, "slow request never reached the facade"
        time.sleep(0.05)
    cfg = peer._relay_config
    # The ring already verifies BOTH keys (the relay boots with both — the
    # verify window). The rotation is the operator flip: fresh mints wear k2
    # now, k1 keeps verifying until its tokens die (the expiry half is
    # test_relay_rotation.py's drill, deliberately not re-run here).
    ring_v2 = relay_control.CapKeyRing(
        [cfg.cap_ring.key_for("k1"), cfg.cap_ring.key_for("k2")],
        active_kid="k2")
    peer._relay_config = relay_control.RelayConfig(
        urls=cfg.urls, realm=cfg.realm, attachment_key=cfg.attachment_key,
        cap_ring=ring_v2, attachment_audience=cfg.attachment_audience,
        capability_type=cfg.capability_type,
        capability_audience=cfg.capability_audience, quota=cfg.quota)
    holder.join(timeout=15)
    facade.slow_seconds = 0.0
    assert settled == ["ok"], f"in-flight stream broke across the rotation: {settled}"
    cap_v2, _ = peer._relay_config.mint_capability_for(
        account_id=peer._account_id, device_id=peer._device_id,
        target_id=DAEMON_ID, relay_id=RELAY_ID, region="local",
        scopes=["terminal.proxy"])
    assert relay_control.peek_capability_kid(cap_v2) == "k2"
    assert peer.refresh()["device_id"] == DEVICE
    # Identity is the daemon_id (DR-2): the broker-side key rotation changes
    # nothing about who this peer IS.
    assert peer.node_id == NODE_ID
    row = {x["peer"]: x for x in broker.membership_snapshot()}[MESH_URL]
    assert row.get("transport") == "mesh"

    # -- (8) quota exhaustion: named relay_quota, distinct from unhealth ----
    harness.set_quota(1)                       # one stream slot per account
    facade.slow_seconds = 3.0
    blocked = []

    def hold_slow_again():
        try:
            peer._http(f"{peer.base}/slow", timeout=30)
            blocked.append("ok")
        except Exception as e:                    # noqa: BLE001 - asserted below
            blocked.append(e)

    holder2 = threading.Thread(target=hold_slow_again, daemon=True)
    holder2.start()
    deadline = time.monotonic() + 10
    while not facade.saw("GET", "/livestack/slow"):
        assert time.monotonic() < deadline, "slow request never reached the facade"
        time.sleep(0.05)
    try:
        with pytest.raises(RelayQuotaExceeded) as ei:
            peer.refresh()
        assert ei.value.degradation == "relay_quota"
        assert "relay_quota" in str(ei.value)
        # The membership row carries the SAME named degradation — 429 quota,
        # not tunnel death, and not node unhealth: the loopback facade (what
        # the tunnel terminates on) answers throughout. Polled, not read
        # once: within the re-probe backoff a snapshot skips the peer, and
        # only a snapshot that actually probed carries the named failure.
        def _quota_row():
            broker.snapshot()
            r = {x["peer"]: x for x in broker.membership_snapshot()}[MESH_URL]
            return r if r.get("degradation") == "relay_quota" else None
        row = _wait_for(_quota_row)
        assert row is not None, "relay_quota never surfaced on the membership row"
        assert row["state"] == "suspect"
        assert row.get("degradation") != "mesh_tunnel_down"
        assert facade_answers(facade.base_url) is True
    finally:
        harness.set_quota(0)
        facade.slow_seconds = 0.0
    holder2.join(timeout=15)
    assert blocked == ["ok"], f"held stream broke across the quota episode: {blocked}"
    assert _wait_for(lambda: _probe_ok(peer))

    def _clean_row():
        broker.snapshot()
        r = {x["peer"]: x for x in broker.membership_snapshot()}[MESH_URL]
        return r if r["state"] == "fresh" and "degradation" not in r else None
    assert _wait_for(_clean_row) is not None, "membership row never recovered"
    assert peer.refresh()["device_id"] == DEVICE

    # -- ledger joins: the whole journey reconciles in one query ------------
    records = _records(journey.ledger)
    admits = [r for r in records if r.get("decision") == "admit"]
    assert len(admits) == 1, f"expected one admit record: {admits}"
    admit = admits[0]
    assert admit["request"]["owner"] == JOURNEY_OWNER
    assert admit["outcome"]["status"] == "ok"
    assert admit["outcome"]["served_by"], "the grant must name the serving node"
    # Join 1: fleet admit -> hosted lease, both directions. (The lease also
    # carries the admit's decision_id when the broker runs a policy runtime;
    # this broker runs none, so the lease_id is itself the join.)
    assert admit["outcome"]["lease_id"] == lease_id
    lease = broker.hosted_leases.get(lease_id)
    assert lease is not None, "the admit's lease must still be booked"
    assert lease.get("decision_id") in (None, admit.get("decision_id"))
    # Join 2: the host-level grant/load/evict records join on the owner, so
    # "who warmed it, who needed the room, who evicted it" is one query.
    grants = [r for r in records
              if r.get("decision") == "grant"
              and r.get("request", {}).get("owner") == JOURNEY_OWNER]
    assert len(grants) == 2, f"two host admits expected: {grants}"
    loads = [r for r in records if r.get("decision") == "load"
             and r.get("kind") == "qwen"]
    assert len(loads) == 1 and loads[0]["request"]["caused_by"] == JOURNEY_OWNER
    evicts_r = [r for r in records if r.get("decision") == "evict"]
    assert len(evicts_r) == 1 and evicts_r[0]["kind"] == "qwen"
    assert evicts_r[0]["request"]["caused_by"] == JOURNEY_OWNER, (
        "the eviction must be attributable to the journey that needed the room")
    # And no eviction anywhere in the journey carried a transport degradation:
    # every eviction cites a non-transport cause (task 8.1's obligation, now
    # asserted across the whole consolidated journey).
    assert all("transport_degradation" not in r["request"] for r in evicts_r)


def _probe_ok(peer) -> bool:
    try:
        peer.refresh()
        return True
    except Exception:                         # noqa: BLE001 - boolean probe
        return False
