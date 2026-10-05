"""Storage-bounded handler registry with burst headroom: real HTTP, SQLite and files.

The registry no longer caps releases per handler. It bounds bytes, manifest metadata,
unreferenced candidates and the total release count, and relieves a bound by evicting
aged, unreferenced releases (all-or-nothing) before it refuses by name.
"""
import hashlib
import io
import json
import sqlite3
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread

import pytest

from livestack_node.workloads import handler_registry
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.config import load_config
from livestack_node.workloads.handler_registry import HandlerReleaseRegistry
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer

FIXTURE = Path(__file__).parent/'fixtures'/'harmony-handler-release-v1'
HOUR = 3600


def package(tmp_path, version):
    manifest = json.loads((FIXTURE/'manifest.json').read_text())
    manifest['handler_id'] = 'test.v1'
    manifest['release_version'] = version
    checked = validate_manifest(manifest)
    entry = manifest['files'][0]
    archive_path = tmp_path/f'{version}.tar'
    with tarfile.open(archive_path, 'w', format=tarfile.USTAR_FORMAT) as archive:
        content = (FIXTURE/entry['path']).read_bytes()
        info = tarfile.TarInfo(entry['path'])
        info.mode, info.size, info.mtime, info.uid, info.gid = entry['mode'], len(content), 0, 0, 0
        info.uname = info.gname = ''
        archive.addfile(info, io.BytesIO(content))
    return manifest, checked['release_digest'], archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest()


@pytest.fixture
def make_authority(tmp_path):
    """Factory: a live authority whose policy carries the given burst age (None = eviction off)."""
    started = []

    def build(burst):
        store = WorkloadStore(tmp_path/f'authority-{len(started)}'/'jobs.db', handlers={'test.v1'})
        principals = [Principal('operator', 'o'*32, 'admin', ('test.v1',)),
                      Principal('caller', 'c'*32, 'caller', ('test.v1',)),
                      Principal('worker', 'w'*32, 'worker', worker='w1', host='host1')]
        policy = {'revision': 'burst-test', 'retention_seconds': 24*HOUR,
                  'handlers': {'test.v1': {'runtime_ids': ['node22'], 'backends': ['native']}}}
        if burst is not None:
            policy['burst_min_age_seconds'] = burst
        server = WorkloadServer(('127.0.0.1', 0), store, principals, handler_release_policy=policy)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        clients = {name: WorkloadClient(f'http://127.0.0.1:{server.server_port}', token)
                   for name, token in (('operator', 'o'*32), ('caller', 'c'*32), ('worker', 'w'*32))}
        started.append((server, thread, clients))
        return server, clients

    yield build
    for server, thread, clients in started:
        for client in clients.values():
            client.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def stage(client, bundle):
    manifest, digest, archive, archive_digest = bundle
    assert InputTransfer(client).put(archive)['digest'] == archive_digest
    return client.request('handler-releases/stage', dict(manifest=manifest, release_digest=digest,
        archive_digest=archive_digest, archive_bytes=archive.stat().st_size))


def activate(client, digest, generation, request_id):
    return client.request('handler-releases/activate', dict(handler_id='test.v1', release_digest=digest,
        expected_generation=generation, request_id=request_id))


def present(clients):
    status = clients['operator'].request('handler-releases/status')
    return {row['release_digest'] for row in status['releases']}


def events(clients):
    return clients['operator'].request('handler-releases/status')['events']


def age(server, releases, seconds_ago):
    """Make releases `seconds_ago` old; strictly increasing so eviction order is deterministic."""
    now = server.store.clock()
    with server.store.transaction() as db:
        for index, release in enumerate(releases):
            db.execute('UPDATE handler_releases SET created=? WHERE release_digest=?',
                       (now-seconds_ago+index, release[1]))


def fill_to(monkeypatch, releases):
    """Cap the registry at exactly len(releases) archives (all archives are the same size)."""
    sizes = {release[2].stat().st_size for release in releases}
    assert len(sizes) == 1
    monkeypatch.setattr(handler_registry, 'MAX_REGISTRY_BYTES', sizes.pop()*len(releases))


def test_burst_of_eight_releases_for_one_handler_is_accepted_within_storage_bounds(make_authority, tmp_path):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'burst-{index}') for index in range(8)]
    for release in releases:
        assert stage(clients['operator'], release)['state'] == 'staged'
    assert present(clients) == {release[1] for release in releases}
    outcomes = {row['outcome'] for row in events(clients)}
    assert 'handler_release_count_capacity' not in outcomes
    status = clients['operator'].request('handler-releases/status')
    assert status['policy']['limits']['releases_per_handler'] is None
    assert status['policy']['burst_min_age_seconds'] == HOUR


def test_byte_pressure_evicts_the_oldest_aged_unreferenced_release(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'bytes-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, 2*HOUR)
    newest = package(tmp_path, 'bytes-new')
    result = stage(clients['operator'], newest)
    assert result['evicted'] == 1
    assert present(clients) == {releases[1][1], releases[2][1], newest[1]}
    evicted = [row for row in events(clients) if row['outcome'] == 'evicted_for_byte_capacity']
    assert [row['release_digest'] for row in evicted] == [releases[0][1]]
    assert evicted[0]['handler_id'] == 'test.v1' and evicted[0]['bytes'] > 0


def test_eviction_never_touches_default_rollback_job_or_worker_referenced_releases(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    a, b, c, d, e, f, g = (package(tmp_path, f'safe-{name}') for name in 'abcdefg')
    fill_to(monkeypatch, [a, b, c, d, e])
    for release in (a, b, c, d, e):
        stage(clients['operator'], release)
    activate(clients['operator'], a[1], 0, '1'*32)
    activate(clients['operator'], b[1], 1, '2'*32)          # a is the rollback target, b the default
    source = b'safe-input'
    input_digest = hashlib.sha256(source).hexdigest()
    path = tmp_path/'safe-input'
    path.write_bytes(source)
    InputTransfer(clients['caller']).put(path)
    clients['caller'].submit(dict(version=1, key='pins-c', handler='test.v1', input_digest=input_digest,
        need={'cpu': 1, 'memory_bytes': 128*1024**2},
        handler_release={'selection': 'exact', 'release_digest': c[1]}))     # c: a queued job
    capacity = dict(cpu=2, memory_bytes=2*1024**3, disk_bytes=20*1024**3)
    identity = dict(handler_id='test.v1', release_digest=d[1], execution_contract=1,
        payload_schema=d[0]['payload_schema'], result_schema=d[0]['result_schema'])
    clients['worker'].request('worker/report', dict(boot='boot-1', report=dict(
        capacity=capacity, available=capacity, labels={}, handlers=['test.v1'], ready=True,
        handler_inventory={'generation': 2, 'defaults': {'test.v1': d[1]}, 'releases': [identity]})))
    age(server, [a, b, c, d, e], 2*HOUR)                    # e alone is unreferenced
    assert stage(clients['operator'], f)['evicted'] == 1
    assert present(clients) == {a[1], b[1], c[1], d[1], f[1]}
    # Nothing evictable now: f is fresh and every other release is referenced. Refuse, delete nothing.
    with pytest.raises(WorkloadError, match='handler_registry_byte_capacity'):
        stage(clients['operator'], g)
    assert present(clients) == {a[1], b[1], c[1], d[1], f[1]}
    assert events(clients)[0]['outcome'] == 'handler_registry_byte_capacity'


def test_fresh_releases_are_protected_by_the_minimum_age(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'fresh-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, HOUR//2)                          # younger than the burst age
    with pytest.raises(WorkloadError, match='handler_registry_byte_capacity'):
        stage(clients['operator'], package(tmp_path, 'fresh-new'))
    assert present(clients) == {release[1] for release in releases}


def test_unset_burst_age_disables_eviction_and_refuses_by_name(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(None)
    releases = [package(tmp_path, f'off-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, 2*HOUR)
    with pytest.raises(WorkloadError, match='handler_registry_byte_capacity'):
        stage(clients['operator'], package(tmp_path, 'off-new'))
    assert present(clients) == {release[1] for release in releases}


def test_unavailable_reference_evidence_evicts_nothing(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'evidence-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, 2*HOUR)

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError('simulated reference evidence outage')
    monkeypatch.setattr(server.handler_registry, '_unreferenced', unavailable)
    with pytest.raises(WorkloadError, match='handler_registry_byte_capacity'):
        stage(clients['operator'], package(tmp_path, 'evidence-new'))
    monkeypatch.undo()
    assert present(clients) == {release[1] for release in releases}
    assert 'handler_release_eviction_evidence_unavailable' in {row['outcome'] for row in events(clients)}


def test_restaging_an_existing_release_is_idempotent_and_evicts_nothing(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'idem-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, 2*HOUR)
    again = stage(clients['operator'], releases[0])
    assert again['idempotent'] is True and 'evicted' not in again
    assert present(clients) == {release[1] for release in releases}


def test_two_stages_racing_for_capacity_never_exceed_the_byte_bound(make_authority, tmp_path, monkeypatch):
    server, clients = make_authority(HOUR)
    releases = [package(tmp_path, f'race-{index}') for index in range(3)]
    fill_to(monkeypatch, releases)
    for release in releases:
        stage(clients['operator'], release)
    age(server, releases, 2*HOUR)
    newcomers = [package(tmp_path, f'race-new-{index}') for index in range(2)]
    for release in newcomers:
        assert InputTransfer(clients['operator']).put(release[2])['digest'] == release[3]

    def stage_uploaded(release):
        manifest, digest, archive, archive_digest = release
        return clients['operator'].request('handler-releases/stage', dict(manifest=manifest,
            release_digest=digest, archive_digest=archive_digest, archive_bytes=archive.stat().st_size))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(stage_uploaded, newcomers))
    assert all(result['state'] == 'staged' for result in results)
    kept = present(clients)
    assert {release[1] for release in newcomers} <= kept and len(kept) == 3
    with server.store.transaction() as db:
        total = db.execute('SELECT sum(archive_bytes) FROM handler_releases').fetchone()[0]
    assert total <= handler_registry.MAX_REGISTRY_BYTES


def test_eviction_database_work_does_not_grow_with_the_number_of_releases(make_authority, tmp_path, monkeypatch):
    def statements_to_evict_one(count):
        server, clients = make_authority(HOUR)
        releases = [package(tmp_path, f'work-{count}-{index}') for index in range(count)]
        fill_to(monkeypatch, releases)
        for release in releases:
            stage(clients['operator'], release)
        age(server, releases, 2*HOUR)
        newcomer = package(tmp_path, f'work-{count}-new')
        assert InputTransfer(clients['operator']).put(newcomer[2])['digest'] == newcomer[3]
        trace, original = [], server.store.connect

        def traced():
            connection = original()
            connection.set_trace_callback(trace.append)
            return connection
        monkeypatch.setattr(server.store, 'connect', traced)
        manifest, digest, archive, archive_digest = newcomer
        result = clients['operator'].request('handler-releases/stage', dict(manifest=manifest,
            release_digest=digest, archive_digest=archive_digest, archive_bytes=archive.stat().st_size))
        monkeypatch.setattr(server.store, 'connect', original)
        assert result['evicted'] == 1
        return len([sql for sql in trace if 'WITH referenced(digest)' in sql]), len(trace)

    assert statements_to_evict_one(4) == statements_to_evict_one(14)


def test_policy_rejects_a_burst_age_below_the_floor_or_beyond_retention(tmp_path):
    store = WorkloadStore(tmp_path/'policy'/'jobs.db', handlers={'test.v1'})
    handlers = {'test.v1': {'runtime_ids': ['node22'], 'backends': ['native']}}
    for burst in (60, HOUR-1, 25*HOUR, True, '3600'):
        with pytest.raises(ValueError, match='burst eviction age'):
            HandlerReleaseRegistry(store, None, {'retention_seconds': 24*HOUR, 'burst_min_age_seconds': burst,
                                                 'handlers': handlers})
    assert HandlerReleaseRegistry(store, None, {'retention_seconds': 24*HOUR, 'burst_min_age_seconds': HOUR,
                                                'handlers': handlers}).burst_min_age_seconds == HOUR
    with pytest.raises(ValueError, match='invalid handler release policy'):
        HandlerReleaseRegistry(store, None, {'burst_age': HOUR, 'handlers': handlers})


def test_config_schema_fails_closed_on_the_policy_without_echoing_values(tmp_path):
    base = {'state_dir': str(tmp_path), 'handlers': ['test.v1'], 'principals': []}
    ok = tmp_path/'ok.json'
    ok.write_text(json.dumps(dict(base, handler_release_policy={'revision': 'r', 'retention_seconds': 86400,
                                                                 'burst_min_age_seconds': 3600})))
    assert load_config(ok)['handler_release_policy']['burst_min_age_seconds'] == 3600
    # Distinctive bad values: the message may name the floor (3600) but must never echo the input.
    for bad, secret in (({'burst_min_age_seconds': 4217-4000}, '217'), ({'burst_min_age_second': 7777}, '7777'),
                        ({'burst_min_age_seconds': '7331'}, '7331')):
        path = tmp_path/'bad.json'
        path.write_text(json.dumps(dict(base, handler_release_policy=bad)))
        with pytest.raises(ValueError, match='handler_release_policy') as raised:
            load_config(path)
        assert secret not in str(raised.value)
