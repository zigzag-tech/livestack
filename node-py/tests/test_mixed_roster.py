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

import pytest

import mesh_stack
from mesh_stack import (CALLER_ACCOUNT, CALLER_DEVICE, CAP_AUDIENCE, CAP_TYPE,
                        Facade, MeshKeys, REALM, RELAY_ID, RelayHarness,
                        start_attach, stop_attach, wait_attached)

mesh_stack.load_meshlink()          # skips the whole module, loudly, if absent

from livestack_node import relay_control
from livestack_node.hostbroker import HostBroker, peer_key
from livestack_node.hostd import DEFAULT_FOOTPRINTS, DEFAULT_PRIORITIES, make_peer
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
