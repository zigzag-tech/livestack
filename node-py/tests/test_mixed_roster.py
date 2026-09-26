"""Mixed roster: one http:// RestPeer and one mesh:// MeshPeer in the SAME
broker (openspec task 5.2 — the incremental-migration proof).

Both peers are real dials: the HTTP peer is a RestPeer against a loopback
facade, the mesh peer reaches its facade through the real relay engine and a
real mesh_outbound_py attachment (see mesh_stack.py). The assertions are about
the broker's treatment: both are opaque URL-keyed records — membership,
planning, and device discovery must not look inside either URL.
"""
from __future__ import annotations

import json
import os
import time

import pytest

import mesh_stack
from mesh_stack import (CALLER_ACCOUNT, CALLER_DEVICE, CAP_AUDIENCE, CAP_TYPE,
                        Facade, MeshKeys, REALM, RELAY_ID, RelayHarness,
                        start_attach, stop_attach, wait_attached)

mesh_stack.load_meshlink()          # skips the whole module, loudly, if absent

from livestack_node import relay_control
from livestack_node.hostbroker import HostBroker, peer_key
from livestack_node.hostd import DEFAULT_FOOTPRINTS, DEFAULT_PRIORITIES, make_peer
from livestack_node.ledger import JsonlLedger, validate
from livestack_node.mesh_peer import _LoopThread
from livestack_node.planner import Request

GB = 1_000_000_000
MESH_DAEMON = "gpu-box-9"
MESH_URL = f"mesh://{REALM}/{MESH_DAEMON}/livestack"


@pytest.fixture(scope="module")
def roster():
    ring = relay_control.CapKeyRing(
        [relay_control.CapKey(k["kid"], k["secret"])
         for k in MeshKeys.cap_ring_keys], active_kid="k1")
    harness = RelayHarness(MeshKeys.cap_ring_keys)
    # Two different nodes on two different (virtual) hosts, one per scheme.
    http_facade = Facade(host_id="host-plain", device_id="dev-plain-1",
                         node_id="plain-node",
                         units=[{"kind": "asr", "residency": 0, "busy": False,
                                 "footprint": {"vram_bytes": 5 * GB}}])
    mesh_facade = Facade(host_id="host-mesh", device_id="dev-mesh-1",
                         node_id="mesh-node",
                         units=[{"kind": "qwen", "residency": 0, "busy": False,
                                 "footprint": {"vram_bytes": 9 * GB}}])
    cfg = relay_control.RelayConfig(urls=(harness.relay_url,), realm=REALM,
                                    cap_ring=ring, capability_type=CAP_TYPE,
                                    capability_audience=CAP_AUDIENCE)
    loop = _LoopThread()
    att = start_attach(loop, harness, mesh_facade, MESH_DAEMON)

    # The REAL factory, env config included: LIVESTACK_RELAY_IDS maps the relay
    # URL to the relay_id the caps must name.
    os.environ["LIVESTACK_RELAY_IDS"] = json.dumps(
        {harness.relay_url: RELAY_ID})
    peers = [
        make_peer(http_facade.base_url, priorities=DEFAULT_PRIORITIES,
                  fallback_footprints=DEFAULT_FOOTPRINTS),
        make_peer(MESH_URL, priorities=DEFAULT_PRIORITIES,
                  fallback_footprints=DEFAULT_FOOTPRINTS,
                  relay_config=cfg, relay_account=CALLER_ACCOUNT,
                  relay_device=CALLER_DEVICE),
    ]
    broker = HostBroker(devices=None, peers=peers,
                        device_config={"dev-plain-1": {"vram_bytes": 24 * GB,
                                                       "reserved": 0},
                                       "dev-mesh-1": {"vram_bytes": 24 * GB,
                                                      "reserved": 0}},
                        clock=lambda: 1000.0)
    try:
        wait_attached(peers[1])
        yield type("Roster", (), {"broker": broker, "peers": peers,
                                  "http_facade": http_facade,
                                  "mesh_facade": mesh_facade, "loop": loop,
                                  "att": att, "harness": harness})
    finally:
        stop_attach(loop, att)        # before the loop stops: stop schedules tasks
        peers[1].close()
        loop.close()
        http_facade.stop()
        mesh_facade.stop()
        harness.stop()
        os.environ.pop("LIVESTACK_RELAY_IDS", None)


def test_both_peers_are_scheme_selected_and_url_keyed(roster):
    from livestack_node.hostbroker import RestPeer
    from livestack_node.mesh_peer import MeshPeer
    assert isinstance(roster.peers[0], RestPeer)
    assert isinstance(roster.peers[1], MeshPeer)
    # peer_key — the membership/planning/reclaim identity — is the URL record
    # itself for BOTH schemes; nothing is derived from, or looked up inside,
    # either URL.
    assert peer_key(roster.peers[0]) == roster.peers[0].base
    assert peer_key(roster.peers[1]) == MESH_URL


def test_membership_records_carry_transport_join_metadata(roster):
    # Task 5.2's ledger obligation (design.md): the mesh peer's membership
    # row carries transport=mesh; the HTTP peer's row stays unchanged.
    roster.broker.snapshot()
    rows = {r["peer"]: r for r in roster.broker.membership_snapshot()}
    assert rows[MESH_URL].get("transport") == "mesh"
    assert "transport" not in rows[roster.peers[0].base]


def test_snapshot_discovers_both_devices_and_both_units(roster):
    snap = roster.broker.snapshot()
    assert sorted(d.id for d in snap.devices) == ["dev-mesh-1", "dev-plain-1"]
    assert set(snap.units) == {"asr", "qwen"}
    # Each kind is servable only on the device that actually has the node.
    assert snap.units["asr"].servable_on == frozenset({"dev-plain-1"})
    assert snap.units["qwen"].servable_on == frozenset({"dev-mesh-1"})
    assert roster.http_facade.saw("GET", "/livestack/residence")
    assert roster.mesh_facade.saw("GET", "/livestack/residence")


def test_planning_dispatches_each_kind_to_its_own_transport(roster):
    # asr exists only on the HTTP node; qwen only behind the mesh tunnel.
    dev_asr = roster.broker.admit(Request("r-asr", "asr", created_at=1000))
    dev_qwen = roster.broker.admit(Request("r-qwen", "qwen", created_at=1000))
    assert dev_asr == "dev-plain-1"
    assert dev_qwen == "dev-mesh-1"
    # The dispatch actually crossed both transports: each facade saw its warm.
    assert roster.http_facade.saw("POST", "/livestack/model/warm")
    assert roster.mesh_facade.saw("POST", "/livestack/model/warm")
    assert roster.http_facade.resident.get("asr") is True
    assert roster.mesh_facade.resident.get("qwen") is True


# ---------------------------------------------------------------------------
# Phase 8 drills (tasks 8.1/8.2). The naming of a relay-restart failure and
# the survival of the remembered read are pinned by Phase 5's
# test_mesh_peer.py::test_relay_restart_mid_membership_is_named_not_silent;
# these drills extend that proof at the BROKER level rather than re-testing
# it: which membership rung the tunnel-down lands on, that GPU placements
# survive the whole episode, and that a warm over a stalled tunnel surfaces
# as a failure instead of a stuck in-flight. Both drills drive the harness's
# command controls (relay_down/relay_up) — no sleeps standing in for transport
# state, and the suspect re-probe knob is taken at test scale (0.5 s) instead
# of its production default.
# ---------------------------------------------------------------------------


def _drill_broker(roster, ledger=None):
    return HostBroker(devices=None, peers=list(roster.peers),
                      device_config={"dev-plain-1": {"vram_bytes": 24 * GB,
                                                     "reserved": 0},
                                     "dev-mesh-1": {"vram_bytes": 24 * GB,
                                                    "reserved": 0}},
                      mesh_suspect_probe_s=0.5, ledger=ledger)


def test_relay_restart_drill_placements_survive_suspect_zero_evictions(roster, tmp_path):
    ledger = JsonlLedger(str(tmp_path / "drill.jsonl"))
    broker = _drill_broker(roster, ledger)
    mesh_key = MESH_URL
    http_key = roster.peers[0].base

    # Healthy baseline: qwen resident on the mesh peer's card.
    broker.snapshot()
    assert broker.roster.state_of(mesh_key) == "fresh"
    assert broker.admit(Request("drill-1", "qwen", created_at=time.time())) \
        == "dev-mesh-1"
    world = broker.snapshot()
    assert any(p.kind == "qwen" and p.device_id == "dev-mesh-1"
               for p in world.placements)

    roster.harness.relay_down()
    try:
        # First probe across the dead tunnel: demoted on the EVENT, at age 0
        # — the age-based rung would still read fresh here.
        world = broker.snapshot()
        assert broker.roster.state_of(mesh_key) == "suspect"
        row = {r["peer"]: r for r in broker.membership_snapshot()}[mesh_key]
        assert row["state"] == "suspect"
        # Named, attributed, queriable without parsing message text.
        assert row.get("degradation") == "mesh_tunnel_down"
        assert "mesh_tunnel_down" in (row.get("last_error") or "")
        # Placements survive: the remembered read feeds the world, so the
        # planner still sees qwen on the card and warms nothing else.
        assert broker._remembered_peer(mesh_key) is not None
        assert any(p.kind == "qwen" and p.device_id == "dev-mesh-1"
                   for p in world.placements)

        # One fast re-probe window later the broker tries again on its own:
        # still suspect, still placed. (A probe cycle is where the remembered
        # read enters the world; between cycles the roster's backoff defers
        # the dial entirely.)
        time.sleep(0.7)
        world = broker.snapshot()
        assert broker.roster.state_of(mesh_key) == "suspect"
        assert any(p.kind == "qwen" and p.device_id == "dev-mesh-1"
                   for p in world.placements)

        # The http peer's membership is untouched by the mesh peer's tunnel.
        assert broker.roster.state_of(http_key) == "fresh"
        http_row = {r["peer"]: r for r in broker.membership_snapshot()}[http_key]
        assert "degradation" not in http_row
        assert "transport" not in http_row
    finally:
        roster.harness.relay_up()

    # Recovery needs no operator action: the re-attached tunnel is found by
    # the fast re-probe, no broker restart, no re-register.
    wait_attached(roster.peers[1])
    time.sleep(0.7)
    world = broker.snapshot()
    assert broker.roster.state_of(mesh_key) == "fresh"
    assert any(p.kind == "qwen" and p.device_id == "dev-mesh-1"
               for p in world.placements)

    # ZERO evictions across the whole episode, on BOTH transports: a relay
    # restart never evicts a GPU placement.
    assert not roster.mesh_facade.saw("POST", "/livestack/model/evict")
    assert not roster.http_facade.saw("POST", "/livestack/model/evict")

    # Ledger: one observe record per transition, carrying the degradation and
    # every knob that decided it (task 8.1/8.2's knob obligation).
    records = ledger.read()
    assert all(not validate(r) for r in records)
    observes = [r for r in records if r.get("decision") == "observe"
                and mesh_key in (r.get("reason") or "")]
    down = [r for r in observes if "-> suspect" in r["reason"]]
    up = [r for r in observes if "-> fresh" in r["reason"]]
    assert len(down) == 1, f"expected one demotion record, got: {observes}"
    assert len(up) == 1, f"expected one recovery record, got: {observes}"
    membership = down[0]["request"]["membership"]
    assert membership["degradation"] == "mesh_tunnel_down"
    assert membership["mia_after_s"] == 600
    assert membership["probe_every_s"] == 0.5
    assert membership["suspect_after_s"] > 0


def test_warm_over_stalled_tunnel_fails_within_warm_window_not_stuck(roster):
    """Task 8.2: a warm whose dial dies BEFORE the request crosses the tunnel
    surfaces as a FAILED warm — inside the 180 s warm window — and the
    broker drops the in-flight record instead of reserving the card for the
    900 s TTL against a load that never started.

    The drill needs the plan to dispatch a real warm while the tunnel is
    down. A peer that answers nothing defers (its card is not discoverable),
    so the fixture is extended with a second, drill-local node serving TWO
    kinds: qwen stays resident through the episode (so the remembered read
    keeps the card in the world — the same mechanism 8.1 pins), and the warm
    is for the second kind, which is known and servable but not resident.
    The stall itself is the harness's restart control: relay_down refuses
    the warm's dial at the door, relay_up recovers it. Nothing here is a
    sleep standing in for transport state; the one sleep waits out the
    suspect re-probe knob, taken at test scale (0.5 s)."""
    ring = relay_control.CapKeyRing(
        [relay_control.CapKey(k["kid"], k["secret"])
         for k in MeshKeys.cap_ring_keys], active_kid="k1")
    cfg = relay_control.RelayConfig(
        urls=(roster.harness.relay_url,), realm=REALM, cap_ring=ring,
        capability_type=CAP_TYPE, capability_audience=CAP_AUDIENCE)
    facade = Facade(host_id="host-drill", device_id="dev-drill-1",
                    node_id="drill-node",
                    units=[{"kind": "qwen", "residency": 2, "busy": False,
                            "footprint": {"vram_bytes": 9 * GB}},
                           {"kind": "tts", "residency": 2, "busy": False,
                            "footprint": {"vram_bytes": 3 * GB}}])
    att = start_attach(roster.loop, roster.harness, facade, "gpu-box-10")
    url = f"mesh://{REALM}/gpu-box-10/livestack"
    peer = make_peer(url, priorities=DEFAULT_PRIORITIES,
                     fallback_footprints=DEFAULT_FOOTPRINTS,
                     relay_config=cfg, relay_account=CALLER_ACCOUNT,
                     relay_device=CALLER_DEVICE)
    broker = HostBroker(devices=None, peers=[peer],
                        device_config={"dev-drill-1": {"vram_bytes": 24 * GB,
                                                       "reserved": 0}},
                        mesh_suspect_probe_s=0.5)
    warms = lambda: sum(1 for m, p in facade.requests
                        if m == "POST" and p.endswith("/model/warm"))
    try:
        wait_attached(peer)
        # Baseline: qwen resident, so a later remembered read keeps the card
        # discoverable while the tunnel is down.
        broker.snapshot()
        assert broker.admit(Request("drill-q", "qwen", created_at=time.time())) \
            == "dev-drill-1"
        broker.snapshot()
        assert warms() == 1

        roster.harness.relay_down()
        started = time.monotonic()
        try:
            # The probe fails pre-send (tunnel down → suspect, 8.1's rung);
            # the remembered read keeps dev-drill-1 in the world with qwen
            # placed, so the plan dispatches a REAL warm for tts — whose dial
            # then dies at the relay door. RestPeer.warm's contract is a
            # 180 s ceiling; a refused door fails in milliseconds, and the
            # assertion's job is the ceiling.
            granted = broker.admit(Request("stall-1", "tts",
                                           created_at=time.time()))
        finally:
            roster.harness.relay_up()
        elapsed = time.monotonic() - started
        assert granted == "dev-drill-1"
        assert elapsed < 60, f"warm sat {elapsed:.1f}s — outside the warm window"

        # The stalled warm never crossed the tunnel: no new facade warm.
        assert warms() == 1
        # Surfaced as FAILED, not stuck: the dial died before the request
        # reached the node (dispatched=False), so the broker dropped the
        # in-flight record instead of holding it for the 900 s TTL.
        assert ("tts", "dev-drill-1") not in broker._in_flight
        # qwen's placement survived the episode (8.1's property, same flow).
        assert broker.roster.state_of(url) in ("suspect", "fresh")

        # Recovery: the card is honestly free — no loading placement haunts
        # the world — and a re-warm over the healed tunnel succeeds.
        wait_attached(peer)
        time.sleep(0.7)                    # one fast re-probe window
        world = broker.snapshot()
        assert broker.roster.state_of(url) == "fresh"
        assert not any(p.kind == "tts" and p.loading for p in world.placements)
        assert broker.admit(Request("stall-2", "tts", created_at=time.time())) \
            == "dev-drill-1"
        assert warms() == 2
    finally:
        stop_attach(roster.loop, att)
        peer.close()
        facade.stop()
