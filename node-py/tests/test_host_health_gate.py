"""The host's durable health gate in Harmony model placement (host_health.py).

node-h, 2026-11-17: speech nodes on a host whose RAM verdict was open were advertised `ready` in
the NA fleet view and took a live dictation session. hostd had no restart-free hold for local
devices and the facade hardcoded ready=True. These tests pin the gate that replaces the firewall
used that day: one file, read per plan, fail closed, never cleared by measured capacity.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pytest

from livestack_node.host_health import HostHealthFile, read_file
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device, Request

from test_hostbroker import make_host


def write(path, doc):
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    # A distinct mtime even on a coarse filesystem clock: the cache keys on it.
    stamp = time.time() + len(path.read_bytes())
    os.utime(path, (stamp, stamp))


@pytest.mark.parametrize('doc, held', [
    (None, False),
    ({'state': 'eligible', 'owner': 'memory-test', 'reason': 'all-four soak clean'}, False),
    ({'state': 'held', 'owner': 'memory-test', 'reason': 'bit-60 flips'}, True),
    ({'state': 'HELD'}, True),           # not a state this gate knows: closed
    ({'owner': 'x'}, True),
    ('not json', True),
    ('[]', True),
    ('{"state": "eligible", "pad": "' + 'x' * 5000 + '"}', True),  # oversized
])
def test_the_file_fails_closed(tmp_path, doc, held):
    path = tmp_path / 'host-health.json'
    if doc is not None:
        write(path, doc)
    assert read_file(str(path))['held'] is held


def test_the_facade_copy_agrees_with_the_canonical_reader(tmp_path):
    from livestack_node.process_node import host_health_hold
    path = tmp_path / 'host-health.json'
    for doc in (None, {'state': 'eligible'}, {'state': 'held', 'reason': 'r'}, {'state': '?'}, 'x', '{}'):
        if doc is None:
            path.unlink(missing_ok=True)
        else:
            write(path, doc)
        assert (host_health_hold(str(path)) is not None) is read_file(str(path))['held'], doc


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = tmp_path / 'host-health.json'
    monkeypatch.setenv('LIVESTACK_HOST_HEALTH_FILE', str(path))
    return path


def test_a_held_host_places_nothing_new_and_evicts_nothing(gate):
    write(gate, {'state': 'held', 'owner': 'memory-test', 'reason': 'verdict open'})
    broker, asr, tts, chip = make_host()
    assert broker.admit(Request('r1', 'align', created_at=1000)) is None
    assert not any(call[0] in ('warm', 'evict') for peer in (asr, tts, chip) for call in peer.calls)
    # Resident units are left alone (a hold stops NEW placements; it never kills).
    assert tts._resident and chip._resident


def test_releasing_the_file_admits_without_a_restart(gate):
    write(gate, {'state': 'held', 'owner': 'memory-test', 'reason': 'verdict open'})
    broker, *_ = make_host()
    assert broker.admit(Request('r1', 'align', created_at=1000)) is None
    write(gate, {'state': 'eligible', 'owner': 'memory-test', 'reason': 'clean'})
    assert broker.admit(Request('r2', 'align', created_at=1000)) == 'gpu0'


def test_measured_capacity_never_reads_as_health(gate):
    """A DIMM swap grows what the device and host report; the gate does not move."""
    write(gate, {'state': 'held', 'owner': 'memory-test', 'reason': 'verdict open'})
    small = HostBroker(device_config={}, default_capacity={'vram_bytes': 8_000_000_000, 'reserved': 0})
    large = HostBroker(device_config={}, default_capacity={'vram_bytes': 80_000_000_000, 'reserved': 0})
    found = {'gpu0': 'node-h'}
    for broker in (small, large):
        assert broker._resolve_devices(found, {'gpu0': {'vram_bytes': 34_000_000_000}}) == []


def test_hosted_backends_are_not_this_hosts_memory(gate):
    write(gate, {'state': 'held', 'reason': 'verdict open'})
    broker = HostBroker(device_config={'qwen-sg': {'hosted': True, 'concurrency': 4}})
    assert [d.id for d in broker._resolve_devices({'gpu0': 'node-h'})] == ['qwen-sg']


def test_the_fleet_view_and_status_name_the_gate(gate):
    write(gate, {'state': 'held', 'owner': 'memory-test', 'reason': 'verdict open'})
    broker = HostBroker(devices=[], peers=[], clock=lambda: 1000.0)
    health = broker.fleet_view()['vantage_host_health']
    assert health['held'] and health['owner'] == 'memory-test' and health['reason'] == 'verdict open'


def test_the_cache_follows_the_file(tmp_path):
    path = tmp_path / 'h.json'
    reader = HostHealthFile(str(path))
    assert reader.read()['held'] is False
    write(path, {'state': 'held', 'reason': 'a'})
    assert reader.read()['reason'] == 'a'
    write(path, {'state': 'held', 'reason': 'bb'})
    assert reader.read()['reason'] == 'bb'
    path.unlink()
    assert reader.read()['held'] is False


class FacadeGateTest(unittest.IsolatedAsyncioTestCase):
    """The node a router reads: `ready` false with the reason, warm/begin refused, no worker started."""

    async def test_a_held_host_reports_not_ready_and_refuses(self):
        from fastapi import HTTPException
        from livestack_node.process_node import ProcessNode
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        gate = Path(temp.name) / 'host-health.json'
        write(gate, {'state': 'held', 'owner': 'memory-test', 'reason': 'verdict open'})
        node = ProcessNode(dict(host_id='node-h', node_id='node-h:8766', kind='polyasr',
                                device_id='node-h/gpu0', port=0, worker_port=1,
                                argv=[sys.executable, '-c', 'raise SystemExit(3)'], env={},
                                units=[{'kind': 'asr', 'footprint': {'vram_bytes': 100}, 'attributes': {}}],
                                default_unit='asr', idle_seconds=1, host_health_file=str(gate)))
        self.addAsyncCleanup(node.close)
        capability = await node.capability()
        self.assertFalse(capability['ready'])
        self.assertIn('host health held: verdict open', capability['detail'])
        with self.assertRaises(HTTPException):
            await node.warm('asr')
        with self.assertRaises(HTTPException):
            await node.begin('asr')
        self.assertIsNone(node.process)
        write(gate, {'state': 'eligible', 'reason': 'clean'})
        self.assertTrue((await node.capability())['ready'])
