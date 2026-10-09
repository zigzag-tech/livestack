"""Environment request identity and ownership on the real durable authority."""
import hashlib
from io import BytesIO
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.ledger import Decision, JsonlLedger, validate as validate_decision_record
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import Limits, WorkloadError, identity, submission
from livestack_node.workloads.store import WorkloadStore


HANDLERS = {'dev.v1', 'task.v1', 'full.v1', 'publish.v1'}
POLICIES = {
    'dev.v1': {'purpose': 'development', 'profile': 'linux-rust'},
    'task.v1': {'purpose': 'task_e2e', 'profile': 'benchday-task-e2e'},
    'full.v1': {'purpose': 'full_e2e', 'profile': 'full-suite'},
    'publish.v1': {'purpose': 'publishing', 'profile': 'release'},
}
SOURCE = hashlib.sha256(b'captured').hexdigest()


def env_request(key, *, handler='dev.v1', job='job-one', digest=SOURCE, **extra):
    return dict(version=3, key=job, handler=handler, input_digest=digest,
                need={'cpu': 1, 'memory_bytes': 1024},
                environment={'key': key, 'reuse': 'prefer'}, **extra)


def server(tmp_path, *, policies=POLICIES, limits=None):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, limits=limits,
                          environment_handlers=policies)
    api = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', tuple(sorted(HANDLERS))),
        Principal('bob', 'b'*32, 'caller', tuple(sorted(HANDLERS))),
        Principal('admin', 'c'*32, 'admin', tuple(sorted(HANDLERS))),
    ])
    thread = Thread(target=api.serve_forever, daemon=True)
    thread.start()
    return store, api, thread


def request(api, path, *, method='GET', data=None, token='a'*32):
    req = urllib.request.Request(f'http://127.0.0.1:{api.server_port}/v1/workloads/{path}',
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def close(api, thread):
    api.shutdown(); thread.join(timeout=5); api.server_close()


def test_authority_status_exposes_ledger_health_to_admin_only(tmp_path):
    store, api, thread = server(tmp_path)
    try:
        store.decision_ledger = JsonlLedger(str(tmp_path/'missing'/'decisions.jsonl'))
        store.decision_ledger.path = str(tmp_path/'missing-parent'/'decisions.jsonl')
        assert store.record_decision(Decision(
            emitter='job-caller', emitter_id='authority-test', decision='admit')) is False
        status, body = request(api, 'status', token='c'*32)
        assert status == 200
        # The ledger-health keys are exact; status also carries other sections (resource_audit, metrics, storage).
        assert {key: body[key] for key in ('decision_ledger', 'observability_degraded')} == {
            'decision_ledger': {'enabled': True, 'degraded': True, 'reason': 'write_failed'},
            'observability_degraded': ['decision_ledger'],
        }
        assert request(api, 'status')[0] == 403
    finally:
        close(api, thread)


def test_schema_three_environment_capabilities_and_preupload_refusal(tmp_path):
    store, api, thread = server(tmp_path, policies={})
    try:
        client = WorkloadClient(f'http://127.0.0.1:{api.server_port}', 'a'*32)
        caps = client.capabilities()
        assert caps['versions'] == [1, 2, 3, 4]
        assert caps['environments'] == {'version': 1, 'handlers': [], 'forbidden_handlers': []}
        assert caps['scopes']['version'] == 1 and caps['causes']['version'] == 1
        api.blobs.put('alice', SOURCE, len(b'captured'), BytesIO(b'captured'))
        legacy = dict(version=1, key='legacy-http', handler='dev.v1', input_digest=SOURCE,
                      need={'cpu': 1, 'memory_bytes': 1024})
        legacy_first = client.submit(legacy)
        legacy_retry = client.submit(dict(legacy))
        assert legacy_first['id'] == legacy_retry['id']
        assert 'environment_handle' not in legacy_first
        missing_source = 'f'*64
        # The object is deliberately absent. Purpose support must be checked
        # before the HTTP route attempts to open/upload the referenced input.
        status, refusal = request(api, 'jobs', method='POST', data=env_request('task', digest=missing_source))
        assert status == 409 and refusal['error'] == 'environment_unsupported: handler is not enrolled'
        with pytest.raises(WorkloadError, match='environment_unsupported'):
            client.submit(env_request('task', digest=missing_source))
        assert [job['id'] for job in store.list_jobs('alice')] == [legacy_first['id']]
    finally:
        close(api, thread)


def test_disposable_full_request_survives_environment_rollback(tmp_path):
    store, api, thread = server(tmp_path, policies={})
    client = WorkloadClient(f'http://127.0.0.1:{api.server_port}', 'a'*32)
    try:
        api.blobs.put('alice', SOURCE, len(b'captured'), BytesIO(b'captured'))
        full_request = dict(version=1, key='full-after-environment-rollback',
            handler='full.v1', input_digest=SOURCE,
            need={'cpu': 1, 'memory_bytes': 1024})
        accepted = client.submit(full_request)
        assert accepted['id']
        assert 'environment_handle' not in accepted
        assert store.get('alice', accepted['id'])['spec']['handler'] == 'full.v1'

        with pytest.raises(WorkloadError, match='environment_unsupported'):
            client.submit(env_request('task-after-rollback', handler='full.v1'))
        assert [job['id'] for job in store.list_jobs('alice')] == [accepted['id']]
    finally:
        client.close()
        close(api, thread)


def test_old_authority_capability_route_is_a_named_environment_refusal():
    client = WorkloadClient('http://127.0.0.1:1', 'a'*32)
    def missing_capability(_route, _body=None):
        raise WorkloadError('workload request refused', 404)
    client.request = missing_capability
    with pytest.raises(WorkloadError, match='environment_unsupported: authority capability API unavailable'):
        client.capabilities()


def test_schema_three_requires_schema_capability_before_submit():
    client = WorkloadClient('http://127.0.0.1:1', 'a'*32)
    client.capabilities = lambda: {'versions': [1, 2], 'environments': {
        'version': 1, 'handlers': ['dev.v1'], 'forbidden_handlers': []}}
    with pytest.raises(WorkloadError, match='environment_unsupported: schema 3 is unavailable'):
        client.submit(env_request('task'))


def test_environment_key_resolution_is_atomic_durable_and_owner_scoped(tmp_path):
    store, api, thread = server(tmp_path)
    try:
        first = store.submit('alice', env_request('repo/task/linux-rust'))
        handle = first['environment_handle']
        assert len(handle) == 32 and first['spec']['environment'] == {
            'key': 'repo/task/linux-rust', 'reuse': 'prefer'}
        assert first['environment']['state'] == 'empty'
        retry = store.submit('alice', env_request('repo/task/linux-rust'))
        assert retry['id'] == first['id'] and retry['environment_handle'] == handle
        changed = store.submit('alice', env_request('repo/task/linux-rust', job='job-two',
            digest=hashlib.sha256(b'edited source').hexdigest()))
        assert changed['id'] != first['id'] and changed['environment_handle'] == handle
        with pytest.raises(WorkloadError, match='different inputs'):
            store.submit('alice', env_request('repo/task/linux-rust', digest='e'*64))

        reopened = WorkloadStore(store.path, handlers=HANDLERS, environment_handlers=POLICIES)
        close(api, thread)
        reopened.recover()
        api = WorkloadServer(('127.0.0.1', 0), reopened, [
            Principal('alice', 'a'*32, 'caller', tuple(sorted(HANDLERS))),
            Principal('bob', 'b'*32, 'caller', tuple(sorted(HANDLERS))),
        ])
        thread = Thread(target=api.serve_forever, daemon=True)
        thread.start()
        api.blobs.put('alice', SOURCE, len(b'captured'), BytesIO(b'captured'))
        retry_client = WorkloadClient(f'http://127.0.0.1:{api.server_port}', 'a'*32)
        try:
            assert retry_client.submit(env_request('repo/task/linux-rust'))['id'] == first['id']
        finally:
            retry_client.close()
        with reopened.connect() as db:
            jobs_before_inspection = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        status, view = request(api, 'environments/'+handle)
        assert status == 200 and view['handle'] == handle and view['profile'] == 'linux-rust'
        with reopened.connect() as db:
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == jobs_before_inspection
        assert request(api, 'environments/'+handle, token='b'*32)[0] == 404
        foreign = env_request('repo/task/linux-rust', job='foreign', **{})
        foreign['environment'] = {'handle': handle, 'reuse': 'prefer'}
        with pytest.raises(WorkloadError, match='not found'):
            reopened.submit('bob', foreign)
        other_owner = reopened.submit('bob', env_request('repo/task/linux-rust'))
        assert other_owner['environment_handle'] != handle
    finally:
        close(api, thread)


def test_concurrent_environment_submissions_resolve_one_logical_handle(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=POLICIES)
    def submit(index):
        return store.submit('alice', env_request('same-task', job=f'job-{index}',
            digest=hashlib.sha256(f'source-{index}'.encode()).hexdigest()))['environment_handle']
    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = list(pool.map(submit, range(16)))
    assert len(set(handles)) == 1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM task_environments').fetchone()[0] == 1


def register_environment_worker(store, worker, host, *, boot='boot', cpu=4, available_cpu=None,
                                compatibility='b'*64, replicas=None):
    return store.register(worker, host, boot, dict(
        capacity={'cpu': cpu, 'memory_bytes': 2*1024**3},
        available={'cpu': cpu if available_cpu is None else available_cpu,
                   'memory_bytes': 2*1024**3}, labels={'os': 'linux'}, handlers=['dev.v1'], ready=True,
        environment_profiles={'linux-rust': compatibility},
        **({'environment_replicas': replicas} if replicas is not None else {})))


def environment_receipt(handle, generation, digest=SOURCE, *, compatibility='b'*64, outcome='created'):
    return dict(version=1, handle=handle, generation=generation, profile='linux-rust',
        compatibility=compatibility, source_digest=digest, reuse_outcome=outcome,
        reason_code='created', state='parked',
        bytes_used=1024, phase_timings={phase: {'seconds': 0.1} for phase in
            ('queue', 'transfer', 'source_materialization', 'dependencies', 'compile', 'test',
             'execution', 'cleanup')},
        cache_components=[])


def test_writer_is_exclusive_and_environment_receipt_parks_after_cleanup(tmp_path, monkeypatch):
    from livestack_node.workloads import placement

    now = [1000.0]
    ledger = JsonlLedger(str(tmp_path/'workload-decisions.jsonl'))
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, clock=lambda: now[0],
                          environment_handlers=POLICIES, decision_ledger=ledger,
                          decision_emitter_id='authority-test')
    decisions = {}
    original_schedule = placement.schedule
    def capture_schedule(*args, **kwargs):
        plan = original_schedule(*args, **kwargs)
        decisions.update(plan.decisions)
        return plan
    monkeypatch.setattr(placement, 'schedule', capture_schedule)
    register_environment_worker(store, 'worker-a', 'host-a')
    register_environment_worker(store, 'worker-b', 'host-a')
    first = store.submit('alice', env_request('same-writer', job='first'))
    now[0] += 0.1
    second = store.submit('alice', env_request('same-writer', job='second',
        digest=hashlib.sha256(b'next source').hexdigest()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [(worker, pool.submit(store.claim, worker, 'boot'))
                   for worker in ('worker-a', 'worker-b')]
        claims = [(worker, future.result()) for worker, future in pending]
    admitted = [(worker, attempt) for worker, attempt in claims if attempt is not None]
    assert len(admitted) == 1
    attempt_worker, attempt = admitted[0]
    assert attempt['job_id'] == first['id']
    assert isinstance(attempt['decision_id'], str) and len(attempt['decision_id']) == 26
    assert decisions[first['id']]['decision_id'] == attempt['decision_id']
    assert attempt['environment']['handle'] == first['environment_handle']
    assert attempt['environment']['generation'] == 1
    assert store.claim(attempt_worker, 'boot')['decision_id'] == attempt['decision_id']
    assert next(value for worker, value in claims if worker != attempt_worker) is None
    waiting = store.get('alice', second['id'])
    assert waiting['state'] == 'queued' and waiting['reason'] == 'environment_busy'
    assert waiting['attempts'] == []

    receipt = environment_receipt(first['environment_handle'], 1)
    receipt['phase_timings']['compile'] = {'seconds': None, 'reason': 'timer_unavailable'}
    completed = store.complete(attempt_worker, 'boot', attempt['attempt_id'], attempt['fence'],
        input_digest=SOURCE, outcome='succeeded', result={'artifacts': []}, environment_receipt=receipt)
    assert completed['state'] == 'succeeded'
    assert completed['result']['environment_receipt']['reuse_outcome'] == 'created'
    recorded_attempt = next(row for row in completed['attempts']
                            if row['id'] == attempt['attempt_id'])
    completed_receipt = completed['result']['environment_receipt']
    assert completed['id'] == attempt['job_id'] == first['id']
    assert completed['environment_handle'] == attempt['environment']['handle'] == completed_receipt['handle']
    assert recorded_attempt['decision_id'] == attempt['decision_id']
    assert recorded_attempt['environment_generation'] == attempt['environment']['generation'] == \
        completed_receipt['generation'] == 1
    assert recorded_attempt['host'] == 'host-a'
    assert completed['result']['environment_receipt']['phase_timings']['compile'] == {
        'seconds': None, 'reason': 'timer_unavailable'}
    records = ledger.read()
    admission = next(record for record in records if record['decision_id'] == attempt['decision_id'])
    completion_event = next(record for record in records
                            if record.get('parent_decision_id') == attempt['decision_id'])
    assert validate_decision_record(admission) == []
    assert validate_decision_record(completion_event) == []
    assert admission['request']['job_id'] == attempt['job_id'] == first['id']
    assert admission['request']['attempt_id'] == attempt['attempt_id']
    assert admission['request']['environment_handle'] == first['environment_handle']
    assert admission['request']['environment_generation'] == 1
    assert admission['chosen'] == attempt_worker
    assert {candidate['id'] for candidate in admission['candidates']} == {'worker-a', 'worker-b'}
    assert completion_event['outcome']['status'] == 'ok'
    assert completion_event['outcome']['job_id'] == attempt['job_id']
    assert completion_event['outcome']['attempt_id'] == attempt['attempt_id']
    assert completion_event['outcome']['environment_handle'] == first['environment_handle']
    assert completion_event['outcome']['environment_generation'] == 1
    assert completion_event['outcome']['environment_reuse_outcome'] == 'created'
    view = store.get_environment('alice', first['environment_handle'])
    assert view['state'] == 'parked' and view['generation'] == 1
    assert view['replicas'][0]['host'] == 'host-a'
    assert store.complete(attempt_worker, 'boot', attempt['attempt_id'], attempt['fence'],
        input_digest=SOURCE, outcome='succeeded', result={'artifacts': []},
        environment_receipt=receipt)['id'] == first['id'], 'completion replay returns same receipt'
    assert len([record for record in ledger.read()
                if record.get('parent_decision_id') == attempt['decision_id']]) == 1

    # Placement is host-affine and may choose either worker identity on the
    # same physical host; poll both identities to collect the shared-host job.
    ledger.path = str(tmp_path/'missing'/'decisions.jsonl')
    next_attempt = store.claim('worker-a', 'boot') or store.claim('worker-b', 'boot')
    assert next_attempt is not None, f"reason={store.get('alice', second['id'])['reason']!r}"
    assert next_attempt['job_id'] == second['id']
    assert next_attempt['environment']['generation'] == 2
    assert store.status()['observability_degraded'] == ['decision_ledger']
    assert next_attempt['environment']['replicas'][0]['compatibility'] == 'b'*64


@pytest.mark.parametrize(('writer_host', 'should_clean'), [
    ('host-a', False),
    ('host-b', True),
])
def test_registration_preserves_parked_generation_for_same_host_writer(
        tmp_path, writer_host, should_clean):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS,
        limits=Limits(environment_affinity_seconds=0.01), clock=lambda: now[0],
        environment_handlers=POLICIES)
    register_environment_worker(store, 'worker-a', 'host-a')
    register_environment_worker(store, 'worker-b', 'host-a')
    if writer_host == 'host-b':
        register_environment_worker(store, 'worker-c', 'host-b')

    first = store.submit('alice', env_request('handoff', job='first'))
    first_attempt = store.claim('worker-a', 'boot')
    assert first_attempt['environment']['generation'] == 1
    store.complete('worker-a', 'boot', first_attempt['attempt_id'], first_attempt['fence'],
        input_digest=SOURCE, outcome='succeeded', result={'artifacts': []},
        environment_receipt=environment_receipt(first['environment_handle'], 1))

    replica = dict(handle=first['environment_handle'], profile='linux-rust',
        compatibility='b'*64, generation=1, state='parked', bytes_used=1024, last_used=1000.0)
    assert register_environment_worker(store, 'worker-b', 'host-a', replicas=[replica])['ready'] is True

    if writer_host == 'host-b':
        register_environment_worker(store, 'worker-a', 'host-a', available_cpu=0, replicas=[replica])
        register_environment_worker(store, 'worker-b', 'host-a', available_cpu=0, replicas=[replica])

    second = store.submit('alice', env_request('handoff', job='second',
        digest=hashlib.sha256(b'next source').hexdigest()))
    if writer_host == 'host-a':
        writer = store.claim('worker-a', 'boot')
    else:
        assert store.claim('worker-c', 'boot') is None
        now[0] += 1
        writer = store.claim('worker-c', 'boot')
        assert writer is not None, f"reason={store.get('alice', second['id'])['reason']!r}"
    assert writer['job_id'] == second['id']
    assert writer['environment']['generation'] == 2

    registration = register_environment_worker(store, 'worker-b', 'host-a', replicas=[replica])
    cleanup = registration['environment_cleanup']
    if should_clean:
        assert cleanup == [dict(handle=first['environment_handle'], generation=1)]
        with store.connect() as db:
            assert db.execute('SELECT 1 FROM task_environment_replicas WHERE handle=? AND host=?',
                (first['environment_handle'], 'host-a')).fetchone() is None
    else:
        assert cleanup == []
        with store.connect() as db:
            assert db.execute('SELECT generation FROM task_environment_replicas WHERE handle=? AND host=?',
                (first['environment_handle'], 'host-a')).fetchone()[0] == 1


def test_attempt_decision_id_column_migrates_additively(tmp_path):
    path = tmp_path/'jobs.sqlite'
    store = WorkloadStore(path, handlers=HANDLERS, environment_handlers=POLICIES)
    register_environment_worker(store, 'worker-a', 'host-a')
    job = store.submit('alice', env_request('legacy-attempt'))
    old_attempt = store.claim('worker-a', 'boot')
    assert old_attempt['job_id'] == job['id']

    with store.connect() as db:
        db.execute('ALTER TABLE attempts DROP COLUMN decision_id')
    reopened = WorkloadStore(path, handlers=HANDLERS, environment_handlers=POLICIES)
    status = reopened.get('alice', job['id'])
    assert status['attempts'][0]['decision_id'] is None
    assert reopened.claim('worker-a', 'boot')['attempt_id'] == old_attempt['attempt_id']


def test_independent_environments_can_be_admitted_concurrently(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=POLICIES)
    register_environment_worker(store, 'worker-a', 'host-a')
    register_environment_worker(store, 'worker-b', 'host-b')
    first = store.submit('alice', env_request('parallel-a', job='parallel-a'))
    second = store.submit('alice', env_request('parallel-b', job='parallel-b'))

    first_attempt = store.claim('worker-a', 'boot')
    second_attempt = store.claim('worker-b', 'boot')
    assert first_attempt is not None and second_attempt is not None
    assert {first_attempt['job_id'], second_attempt['job_id']} == {first['id'], second['id']}
    assert first_attempt['environment']['handle'] != second_attempt['environment']['handle']
    assert first_attempt['environment']['generation'] == second_attempt['environment']['generation'] == 1


def test_affinity_wait_is_durable_bounded_and_holds_no_compute_claim(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS,
        limits=Limits(environment_affinity_seconds=15), clock=lambda: now[0],
        environment_handlers=POLICIES)
    # A compatible parked replica is on host-a. It is currently full, while
    # host-b is a cold but immediately available compatible destination.
    job = store.submit('alice', env_request('affinity'))
    handle = job['environment_handle']
    with store.transaction() as db:
        db.execute("UPDATE task_environments SET state='parked',generation=1,compatibility=? WHERE handle=?",
                   ('b'*64, handle))
        db.execute("INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,"
                   "bytes_used,last_used,seen) VALUES(?,?,?, ?,1,'parked',100,?,?)",
                   (handle, 'host-a', 'linux-rust', 'b'*64, now[0], now[0]))
    register_environment_worker(store, 'warm', 'host-a', available_cpu=0)
    register_environment_worker(store, 'cold', 'host-b')
    assert store.claim('warm', 'boot') is None
    waiting = store.get('alice', job['id'])
    assert waiting['state'] == 'queued' and waiting['reason'] == 'environment_affinity_wait'
    assert waiting['attempts'] == []
    assert 'eta_seconds' not in waiting and 'estimated_wait_seconds' not in waiting
    with store.connect() as db:
        assert db.execute("SELECT writer_job FROM task_environments WHERE handle=?", (handle,)).fetchone()[0] is None

    # A process restart retains the original affinity deadline; it cannot
    # restart the wait clock or admit a writer while the old preference holds.
    restarted = WorkloadStore(store.path, handlers=HANDLERS,
        limits=Limits(environment_affinity_seconds=15), clock=lambda: now[0],
        environment_handlers=POLICIES)
    restarted.recover()
    register_environment_worker(restarted, 'warm', 'host-a', available_cpu=0)
    register_environment_worker(restarted, 'cold', 'host-b')
    now[0] += 14
    assert restarted.claim('warm', 'boot') is None
    assert restarted.get('alice', job['id'])['reason'] == 'environment_affinity_wait'
    now[0] += 2
    attempt = restarted.claim('cold', 'boot')
    assert attempt['job_id'] == job['id']
    assert attempt['environment']['generation'] == 2


def test_scheduler_batches_replica_registry_lookup_as_environment_count_grows(tmp_path):
    """The affinity snapshot resolves all queued handles with one registry read."""
    counts = []
    for environment_count in (1, 24):
        case = tmp_path/str(environment_count)
        case.mkdir()
        store = WorkloadStore(case/'jobs.sqlite', handlers=HANDLERS,
            clock=lambda: 1000.0, environment_handlers=POLICIES)
        handles = []
        for index in range(environment_count):
            job = store.submit('alice', env_request(f'batch-{index}', job=f'job-{index}'))
            handles.append(job['environment_handle'])
        with store.transaction() as db:
            for handle in handles:
                db.execute("UPDATE task_environments SET state='parked',generation=1,compatibility=? WHERE handle=?",
                           ('b'*64, handle))
                db.execute("INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,"
                           "bytes_used,last_used,seen) VALUES(?,?,?, ?,1,'parked',100,1000,1000)",
                           (handle, 'host-a', 'linux-rust', 'b'*64))
        register_environment_worker(store, 'worker-a', 'host-a')

        statements = []
        connect = store.connect
        def traced_connect():
            db = connect()
            db.set_trace_callback(statements.append)
            return db
        store.connect = traced_connect
        assert store.claim('worker-a', 'boot') is not None
        replica_reads = [sql for sql in statements if 'FROM task_environment_replicas WHERE handle IN (' in sql]
        counts.append(len(replica_reads))

    assert counts == [1, 1]


def test_worker_replica_reports_enforce_the_sixty_four_entry_bound(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=POLICIES)

    def report(count):
        return dict(capacity={'cpu': 4, 'memory_bytes': 2*1024**3},
            available={'cpu': 4, 'memory_bytes': 2*1024**3}, labels={'os': 'linux'},
            handlers=['dev.v1'], ready=True, environment_profiles={'linux-rust': 'b'*64},
            environment_replicas=[dict(handle=f'{index:032x}', profile='linux-rust',
                compatibility='b'*64, generation=1, state='parked', bytes_used=100, last_used=1000.0)
                for index in range(count)])

    with pytest.raises(WorkloadError, match='invalid environment replica report'):
        store.register('worker', 'host-a', 'boot', report(65))
    assert store.register('worker', 'host-a', 'boot', report(64))['ready'] is True


def test_worker_replica_reports_reconcile_only_declared_profiles(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=POLICIES)
    compatibility = {'linux-rust': 'b'*64, 'benchday-task-e2e': 'c'*64}
    rust = store.submit('alice', env_request('shared-rust', job='shared-rust'))['environment_handle']
    stale_rust = store.submit('alice', env_request('stale-rust', job='stale-rust'))['environment_handle']
    task_e2e = store.submit('alice', env_request('shared-task-e2e', handler='task.v1',
        job='shared-task-e2e', payload={'check_ids': ['hub.one']}))['environment_handle']

    def replica(handle, profile):
        return dict(handle=handle, profile=profile, compatibility=compatibility[profile],
            generation=1, state='parked', bytes_used=1024, last_used=1000.0)

    with store.transaction() as db:
        for handle, profile in ((rust, 'linux-rust'), (stale_rust, 'linux-rust'),
                                (task_e2e, 'benchday-task-e2e')):
            db.execute("UPDATE task_environments SET state='parked',generation=1,compatibility=? WHERE handle=?",
                       (compatibility[profile], handle))

    def register(worker, profiles, replicas):
        report = dict(capacity={'cpu': 4, 'memory_bytes': 2*1024**3},
            available={'cpu': 4, 'memory_bytes': 2*1024**3}, labels={'os': 'linux'},
            handlers=['dev.v1'], ready=True, environment_profiles=profiles, environment_replicas=replicas)
        return store.register(worker, 'host-a', 'boot', report)

    register('worker-all-profiles', compatibility, [
        replica(rust, 'linux-rust'), replica(stale_rust, 'linux-rust'),
        replica(task_e2e, 'benchday-task-e2e')])
    register('worker-partial-profiles', {'linux-rust': compatibility['linux-rust']},
        [replica(rust, 'linux-rust')])

    with store.connect() as db:
        rows = db.execute('SELECT handle,profile FROM task_environment_replicas WHERE host=?',
                          ('host-a',)).fetchall()
    assert {(row['handle'], row['profile']) for row in rows} == {
        (rust, 'linux-rust'), (task_e2e, 'benchday-task-e2e')}

    register('worker-empty-profiles', {}, [])
    with store.connect() as db:
        rows = db.execute('SELECT handle,profile FROM task_environment_replicas WHERE host=?',
                          ('host-a',)).fetchall()
    assert {(row['handle'], row['profile']) for row in rows} == {
        (rust, 'linux-rust'), (task_e2e, 'benchday-task-e2e')}

    with pytest.raises(WorkloadError, match='environment replica profile is not declared'):
        register('worker-invalid-scope', {'linux-rust': compatibility['linux-rust']},
            [replica(task_e2e, 'benchday-task-e2e')])


def test_returning_worker_gets_generation_scoped_cleanup_for_stale_replicas(tmp_path):
    # Unit-only: force both a superseded generation and a replica whose logical
    # row vanished into one bounded authority report; worker HTTP coverage uses
    # the normal replacement-generation lifecycle.
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS,
                          environment_handlers=POLICIES)
    job = store.submit('alice', env_request('returning-worker-cleanup'))
    handle = job['environment_handle']
    with store.transaction() as db:
        db.execute("UPDATE task_environments SET state='parked',generation=2,compatibility=? WHERE handle=?",
                   ('b'*64, handle))

    def replica(replica_handle, generation):
        return dict(handle=replica_handle, profile='linux-rust', compatibility='b'*64,
            generation=generation, state='parked', bytes_used=1024, last_used=1000.0)

    report = register_environment_worker(store, 'returning', 'host-a',
        replicas=[replica(handle, 1), replica('f'*32, 7)])
    assert report['environment_cleanup'] == [
        {'handle': handle, 'generation': 1}, {'handle': 'f'*32, 'generation': 7}]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM task_environment_replicas WHERE host='host-a'").fetchone()[0] == 0

    current = register_environment_worker(store, 'returning', 'host-a',
        replicas=[replica(handle, 2)])
    assert current['environment_cleanup'] == []
    with store.connect() as db:
        row = db.execute("SELECT generation,state FROM task_environment_replicas WHERE host='host-a' AND handle=?",
                         (handle,)).fetchone()
    assert tuple(row) == (2, 'parked')


def test_unknown_replica_compatibility_is_not_a_warm_hit(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS,
        clock=lambda: now[0], environment_handlers=POLICIES)
    job = store.submit('alice', env_request('unknown-compatibility'))
    handle = job['environment_handle']
    with store.transaction() as db:
        db.execute("UPDATE task_environments SET state='parked',generation=1,compatibility=? WHERE handle=?",
                   ('b'*64, handle))
        db.execute("INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,"
                   "bytes_used,last_used,seen) VALUES(?,?,?,NULL,1,'parked',100,?,?)",
                   (handle, 'host-a', 'linux-rust', now[0], now[0]))
    register_environment_worker(store, 'warm-but-full', 'host-a', available_cpu=0)
    register_environment_worker(store, 'cold', 'host-b')

    attempt = store.claim('cold', 'boot')
    assert attempt is not None and attempt['job_id'] == job['id']
    with store.connect() as db:
        assert db.execute("SELECT affinity_started FROM task_environments WHERE handle=?",
                          (handle,)).fetchone()[0] is None


def test_policy_excluded_preferred_host_does_not_delay_cold_placement(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS,
        environment_handlers=POLICIES)
    job = store.submit('alice', env_request('excluded-preferred-host'))
    handle = job['environment_handle']
    with store.transaction() as db:
        db.execute("UPDATE task_environments SET state='parked',generation=1,compatibility=? WHERE handle=?",
                   ('b'*64, handle))
        db.execute("INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,"
                   "bytes_used,last_used,seen) VALUES(?,?,?, ?,1,'parked',100,1000,1000)",
                   (handle, 'host-a', 'linux-rust', 'b'*64))
    register_environment_worker(store, 'warm', 'host-a')
    register_environment_worker(store, 'cold', 'host-b')
    store.bind_principals([
        Principal('warm-worker', 'w'*32, 'worker', worker='warm', host='host-a', claim_enabled=False),
    ])

    attempt = store.claim('cold', 'boot')
    assert attempt is not None and attempt['job_id'] == job['id']
    with store.connect() as db:
        assert db.execute("SELECT affinity_started FROM task_environments WHERE handle=?",
                          (handle,)).fetchone()[0] is None


def test_environment_scope_is_installed_policy_and_requires_bounded_exact_task_ids(tmp_path):
    store, api, thread = server(tmp_path)
    try:
        capabilities = store.capabilities('alice', HANDLERS)['environments']
        assert capabilities['handlers'] == ['dev.v1', 'task.v1']
        assert capabilities['forbidden_handlers'] == ['full.v1', 'publish.v1']
        for handler in ('full.v1', 'publish.v1'):
            with pytest.raises(WorkloadError, match='environment_scope_forbidden'):
                store.submit('alice', env_request('blocked', handler=handler))
        for selection in (None, [], ['hub.one', 'hub.one'], ['hub.*'], ['hub.check'] * 65):
            payload = {} if selection is None else {'check_ids': selection}
            with pytest.raises(WorkloadError, match='bounded exact check IDs'):
                store.submit('alice', env_request('task', handler='task.v1', payload=payload))
        scoped = store.submit('alice', env_request('task', handler='task.v1',
            payload={'check_ids': ['hub.one']}))
        assert scoped['environment']['purpose'] == 'task_e2e'
        assert request(api, 'environments/'+scoped['environment_handle'])[0] == 200
    finally:
        close(api, thread)


def test_task_e2e_optional_allowlist_is_enforced_when_installed(tmp_path):
    policies = dict(POLICIES)
    policies['task.v1'] = {**POLICIES['task.v1'], 'check_ids': ['hub.one', 'hub.two']}
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=policies)
    with pytest.raises(WorkloadError, match='outside its installed allowlist'):
        store.validate_submission('alice', env_request('task', handler='task.v1',
            payload={'check_ids': ['hub.other']}))
    with pytest.raises(WorkloadError, match='outside its installed allowlist'):
        store.validate_submission('alice', env_request('task', handler='task.v1',
            payload={'check_ids': ['hub.one', 'hub.two']}))


def test_development_environment_policy_cannot_be_used_for_release_payload_modes(tmp_path):
    policies = dict(POLICIES)
    policies['dev.v1'] = {**POLICIES['dev.v1'], 'payload_modes': ['test', 'analyze']}
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, environment_handlers=policies)
    with pytest.raises(WorkloadError, match='environment_scope_forbidden'):
        store.validate_submission('alice', env_request('dev', payload={'mode': 'build-linux-release'}))
    accepted = store.validate_submission('alice', env_request('dev', payload={'mode': 'test'}))
    assert accepted['environment'] == {'key': 'dev', 'reuse': 'prefer'}


def test_environment_expiry_recreates_key_and_disabled_retention_fails_closed(tmp_path):
    now = [1000.0]
    limits = Limits(environment_idle_seconds=30, environment_generation_seconds=90)
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers=HANDLERS, limits=limits,
        clock=lambda: now[0], environment_handlers=POLICIES)
    first = store.submit('alice', env_request('expiring'))
    store.withdraw('alice', first['id'])
    now[0] += 31
    with pytest.raises(WorkloadError, match='not found'):
        store.get_environment('alice', first['environment_handle'])
    replacement = store.submit('alice', env_request('expiring', job='job-two'))
    assert replacement['environment_handle'] != first['environment_handle']

    disabled = WorkloadStore(tmp_path/'disabled.sqlite', handlers=HANDLERS,
        limits=Limits(environment_idle_seconds=None), environment_handlers=POLICIES)
    assert disabled.capabilities('alice', HANDLERS)['environments']['handlers'] == []
    with pytest.raises(WorkloadError, match='environment_retention_disabled'):
        disabled.submit('alice', env_request('never-retained'))


def test_environment_client_bounds_view_bytes_and_obeys_request_deadline(tmp_path):
    oversized = WorkloadClient('http://127.0.0.1:1', 'a'*32)
    oversized.request = lambda *_args, **_kwargs: {'large': 'x'*(16*1024)}
    with pytest.raises(WorkloadError, match='environment response exceeds byte limit'):
        oversized.get_environment('a'*32)
    oversized.close()

    store, api, thread = server(tmp_path)
    job = store.submit('alice', env_request('deadline'))
    get_environment = store.get_environment
    def delayed_view(*args, **kwargs):
        time.sleep(0.2)
        return get_environment(*args, **kwargs)
    store.get_environment = delayed_view
    client = WorkloadClient(f'http://127.0.0.1:{api.server_port}', 'a'*32, timeout=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(urllib.error.URLError):
            client.get_environment(job['environment_handle'])
        assert time.monotonic()-started < 0.5
    finally:
        client.close()
        close(api, thread)


def test_legacy_schema_identity_and_environment_reference_are_separate():
    legacy = dict(version=1, key='legacy', handler='dev.v1', input_digest=SOURCE,
                  need={'cpu': 1})
    normalized = submission(legacy, HANDLERS, Limits())
    assert 'environment' not in normalized
    assert identity(normalized) == identity(submission(dict(legacy), HANDLERS, Limits()))
    with pytest.raises(WorkloadError, match='unsupported workload schema'):
        submission(dict(legacy, environment={'key': 'task', 'reuse': 'prefer'}), HANDLERS, Limits())
    with pytest.raises(WorkloadError, match='exactly one key or handle'):
        submission(dict(version=3, key='bad', handler='dev.v1', input_digest=SOURCE,
            need={'cpu': 1}, environment={'key': 'task', 'handle': 'a'*32, 'reuse': 'prefer'}),
            HANDLERS, Limits())


def test_cli_environment_selector_observation_and_opt_out(tmp_path):
    store, api, thread = server(tmp_path)
    data = b'captured'
    store_blob = api.blobs.put('alice', SOURCE, len(data), BytesIO(data))
    assert store_blob['digest'] == SOURCE
    config = tmp_path/'client.json'
    config.write_text(json.dumps({'authority': f'http://127.0.0.1:{api.server_port}', 'token': 'a'*32}))
    request_path = tmp_path/'job.json'
    request_path.write_text(json.dumps(dict(version=1, key='cli-task', handler='dev.v1',
        input_digest=SOURCE, need={'cpu': 1}, payload={})))

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'livestack_node.workloads.cli',
            '--config', str(config), *map(str, args)], env=dict(os.environ),
            capture_output=True, text=True, timeout=10)

    try:
        submitted = cli('submit', request_path, '--environment-key', 'repo/cli/linux-rust', '--json')
        assert submitted.returncode == 0, submitted.stderr
        job = json.loads(submitted.stdout)
        assert job['environment_handle'] and job['spec']['version'] == 3
        request_path.write_text(json.dumps(dict(version=1, key='cli-handle', handler='dev.v1',
            input_digest=SOURCE, need={'cpu': 1}, payload={})))
        by_handle = cli('submit', request_path, '--environment-handle', job['environment_handle'])
        assert by_handle.returncode == 0, by_handle.stderr
        assert json.loads(by_handle.stdout)['environment_handle'] == job['environment_handle']
        observed_job = cli('get', job['id'])
        assert observed_job.returncode == 0, observed_job.stderr
        assert json.loads(observed_job.stdout)['id'] == job['id']
        inspected = cli('environment', 'get', job['environment_handle'])
        assert inspected.returncode == 0, inspected.stderr
        assert json.loads(inspected.stdout)['handle'] == job['environment_handle']

        request_path.write_text(json.dumps(dict(version=1, key='cli-disposable', handler='dev.v1',
            input_digest=SOURCE, need={'cpu': 1}, payload={})))
        disposable = cli('submit', request_path, '--no-environment')
        assert disposable.returncode == 0, disposable.stderr
        assert 'environment_handle' not in json.loads(disposable.stdout)

        request_path.write_text(json.dumps(dict(version=3, key='conflict', handler='dev.v1',
            input_digest=SOURCE, need={'cpu': 1}, environment={'key': 'body', 'reuse': 'prefer'})))
        conflict = cli('submit', request_path, '--environment-key', 'other')
        assert conflict.returncode == 2 and 'conflicts' in conflict.stderr

        request_path.write_text(json.dumps(dict(version=3, key='same-reference', handler='dev.v1',
            input_digest=SOURCE, need={'cpu': 1},
            environment={'key': 'repo/cli/linux-rust', 'reuse': 'prefer'})))
        same = cli('submit', request_path, '--environment-key', 'repo/cli/linux-rust')
        assert same.returncode == 0, same.stderr
        assert json.loads(same.stdout)['environment_handle'] == job['environment_handle']
        opt_out_conflict = cli('submit', request_path, '--no-environment')
        assert opt_out_conflict.returncode == 2 and 'conflicts' in opt_out_conflict.stderr
    finally:
        close(api, thread)
