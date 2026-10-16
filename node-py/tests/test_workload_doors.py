"""`object_relay` as data: the object form is one door, the list form is an ordered list of doors, and a
dead door costs only its own route (transfers resume on the next door). Reuses the real-authority fleet."""
import hashlib
import logging

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.doors import parse_object_relay
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.transfer import InputTransfer

from test_workload_edge_forward import KEY, blob, claim, fleet  # noqa: F401  (fleet is a fixture)


def door(name, url, **extra):
    return dict(name=name, url=url, key='k'*32, **extra)


def test_the_object_form_is_one_door_named_relay_and_none_is_no_door():
    assert parse_object_relay(None) == []
    [one] = parse_object_relay({'url': 'https://door.example/harmony-relay/', 'key': 'k'*32})
    assert (one.name, one.url, one.parallel) == ('relay', 'https://door.example/harmony-relay', 4)


def test_the_list_form_keeps_order_and_names_unnamed_doors():
    doors = parse_object_relay([door('ashburn', 'https://a.example/r', parallel=2),
                                {'url': 'https://b.example/r', 'key': 'z'*32}])
    assert [d.name for d in doors] == ['ashburn', 'door-2'] and [d.parallel for d in doors] == [2, 4]


@pytest.mark.parametrize('spec', [
    [], {}, 'https://x.example', [door('a', 'https://a.example/r')]*2,                      # empty / wrong type / duplicates
    [door('a', 'https://a.example/r'), door('a', 'https://b.example/r')],                  # duplicate name
    [door('A', 'https://a.example/r')], [door('a', 'ftp://a.example/r')],                   # bad name / scheme
    [door('a', 'https://u:p@a.example/r')], [door('a', 'https://a.example/r?x=1')],        # credentials / query
    [dict(door('a', 'https://a.example/r'), extra=1)], [door('a', 'https://a.example/r', parallel=9)],
    [door('a', 'https://a.example/r', parallel=True)], [dict(name='a', url='https://a.example/r')],  # unknown / range / bool / no key
    {'name': 'x', 'url': 'https://a.example/r', 'key': 'k'*32},                             # name is for the list form
    [door('d%d' % i, 'https://h%d.example/r' % i) for i in range(9)],                       # more than 8
])
def test_malformed_object_relay_is_refused_by_name(spec):
    with pytest.raises(WorkloadError):
        parse_object_relay(spec)


def test_a_dead_first_door_falls_through_to_the_second_door_then_the_authority(fleet, caplog):  # noqa: F811
    _s, _t, live = fleet.make()
    source, digest = blob(fleet.root, 300_000)
    caller = WorkloadClient(fleet.upstream, 'a'*32)
    uploaded = InputTransfer(caller).put(source)
    worker, assignment = claim(fleet.upstream, caller, uploaded)
    doors = [dict(name='osaka', client=WorkloadClient('http://127.0.0.1:9', 'w'*32), key=KEY, parallel=4),
             dict(name='ashburn', client=WorkloadClient(live, 'w'*32), key=KEY, parallel=4)]
    transfer = InputTransfer(worker, doors=doors)
    assert transfer.health() == dict(cross_region=True, doors=['osaka', 'ashburn'], single_route=False)
    with caplog.at_level(logging.WARNING):
        got = transfer.get(digest, fleet.root/'w/in.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    trail = [(e['route'], e['outcome']) for e in transfer.last_trail]
    assert trail[0] == ('osaka', 'skipped') and trail[-1] == ('ashburn', 'ok'), trail
    # every door dead: the authority completes it, and the trail says so
    dead = InputTransfer(worker, doors=[dict(d, client=WorkloadClient('http://127.0.0.1:9', 'w'*32)) for d in doors])
    got = dead.get(digest, fleet.root/'w/in2.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert dead.last_trail[-1]['route'] == 'authority' and dead.last_trail[-1]['outcome'] == 'ok'


def test_relay_and_doors_are_exclusive():
    client = WorkloadClient('http://127.0.0.1:1', 'w'*32)
    with pytest.raises(ValueError):
        InputTransfer(client, relay=client, relay_key=KEY, doors=[])
