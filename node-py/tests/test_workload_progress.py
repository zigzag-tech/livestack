"""J.7: the progress channel. A handler reports {phase, detail?, fraction?}
through progress.json; the worker forwards it on heartbeat; the store keeps
only the latest; GET jobs/<id> returns it, absent (not null) when never
reported; a heartbeat without progress leaves the last value in place."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

import pytest

import livestack_node
from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.worker import WorkloadWorker


def submit_spec(key, digest, **extra):
    return dict(version=1, key=key, handler='native.v1', input_digest=digest,
                need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2}, **extra)


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='host1')])
    data = b'input fixture'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('alice', digest, len(data), __import__('io').BytesIO(data))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'w'*32)
    yield client, digest
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


STAMPS = ('reported_at', 'changed_at', 'phase_started_at', 'dropped')


def document(job):
    """The handler's own fields of a job read's progress, without the authority's stamps."""
    return {k: v for k, v in job['progress'].items() if k not in STAMPS}


def running_attempt(api, digest):
    client, _ = api
    report = dict(boot='boot', report=dict(
        capacity={'cpu': 1, 'memory_bytes': 128*1024**2, 'disk_bytes': 64*1024**3},
        available={'cpu': 1, 'memory_bytes': 128*1024**2, 'disk_bytes': 64*1024**3},
        labels={}, handlers=['native.v1'], ready=True))
    client.request('worker/report', report)
    return client.request('worker/claim', {'boot': 'boot'})['assignment']


def test_heartbeat_without_progress_leaves_last_value_and_absent_stays_absent(api):
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    job = caller.submit(submit_spec('one', digest))
    assert 'progress' not in caller.get(job['id']), 'absent, not null, before any report'
    attempt = running_attempt(api, digest)
    assert attempt['job_id'] == job['id']
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    client.request('worker/heartbeat', dict(base, progress={'phase': 'tts', 'detail': 'synth'}))
    assert document(caller.get(job['id'])) == {'phase': 'tts', 'detail': 'synth'}
    client.request('worker/heartbeat', base)  # no progress: the last value stays
    assert document(caller.get(job['id'])) == {'phase': 'tts', 'detail': 'synth'}
    client.request('worker/heartbeat', dict(base, progress={'phase': 'stills', 'fraction': .5}))
    assert document(caller.get(job['id'])) == {'phase': 'stills', 'fraction': .5}
    # A heartbeat carrying malformed progress is refused, not stored.
    with pytest.raises(Exception):
        client.request('worker/heartbeat', dict(base, progress={'phase': 'x', 'fraction': 2}))
    assert document(caller.get(job['id'])) == {'phase': 'stills', 'fraction': .5}
    client.request('worker/complete', dict(base, input_digest=digest,
                                           outcome='succeeded', result={'exit_code': 0}))
    # A handler that never reported stays absent after completion.
    quiet = caller.submit(submit_spec('quiet', digest))
    attempt = running_attempt(api, digest)
    assert attempt['job_id'] == quiet['id']
    client.request('worker/complete', dict(boot='boot', attempt_id=attempt['attempt_id'],
                                           fence=attempt['fence'], input_digest=digest,
                                           outcome='succeeded', result={'exit_code': 0}))
    done = caller.get(quiet['id'])
    assert done['state'] == 'succeeded'
    assert 'progress' not in done


@pytest.fixture
def executing(tmp_path, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux systemd user manager and cgroup v2')
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'native.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='integration', host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = tmp_path/'progress-handler.py'
    package_parent = str(Path(livestack_node.__file__).resolve().parent.parent)
    script.write_text(f'''import os, sys, time
sys.path.insert(0, {package_parent!r})
from livestack_node.workloads import lease_helper
lease_helper.report_progress('tts', detail='synthesizing', fraction=.4)
time.sleep(.5)
lease_helper.report_progress('stills')
time.sleep(.5)
''')
    url = f'http://127.0.0.1:{server.server_port}'
    config = dict(authority=url, token='w'*32, worker='integration',
        state_dir=str(tmp_path/'state'), workspace=str(tmp_path/'workspace'),
        require_dedicated_filesystem=False, lease_interval=.1,
        capacity={'cpu':1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        memory_reserve_bytes=0, disk_reserve_bytes=0,
        environment={'PATH':'/usr/bin:/bin'},
        handlers={'native.v1': dict(argv=[sys.executable, str(script)], outputs=[])})
    caller = WorkloadClient(url, 'a'*32)
    source = tmp_path/'source'
    source.mkdir()
    (source/'input').write_text('captured bytes')
    capture(source, ['input'], tmp_path/'source.tar')
    digest = InputTransfer(caller).put(tmp_path/'source.tar')['digest']
    yield caller, config, digest
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_polling_caller_reads_progress_in_report_order(executing):
    caller, config, digest = executing
    job = caller.submit(submit_spec('progress', digest))
    worker = WorkloadWorker(config)
    seen = []
    try:
        stepping = Thread(target=worker.step)
        stepping.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and stepping.is_alive():
            current = caller.get(job['id']).get('progress')
            if current and (not seen or seen[-1] != current):
                seen.append(current)
            if current and current['phase'] == 'stills' and not stepping.is_alive():
                break
            time.sleep(.05)
        stepping.join(timeout=60)
        assert not stepping.is_alive()
    finally:
        worker.close()
    phases = [p['phase'] for p in seen]
    assert 'tts' in phases and 'stills' in phases
    assert phases.index('tts') < phases.index('stills'), f'read back in order: {seen}'
    tts = next(p for p in seen if p['phase'] == 'tts')
    assert tts['detail'] == 'synthesizing' and tts['fraction'] == .4
    assert document(caller.get(job['id'])) == {'phase': 'stills'}
    assert caller.get(job['id'])['state'] == 'succeeded'


# ---- contract v1 (openspec attempt-progress-reporting) -------------------------------------------

def test_v1_fields_are_authority_stamped_and_identical_reports_do_not_move_changed_at(api):
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    job = caller.submit(submit_spec('v1', digest))
    attempt = running_attempt(api, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    assert 'attempt' in caller.get(job['id']) and 'progress' not in caller.get(job['id'])
    before = time.time()
    run = {'phase': 'run', 'done': 37, 'total': 120, 'unit': 'assertions', 'message': 'lane startup'}
    client.request('worker/heartbeat', dict(base, progress=run))
    first = caller.get(job['id'])
    assert document(first) == run
    stamps = {k: first['progress'][k] for k in STAMPS[:3]}
    assert all(before - 1 <= v <= time.time() + 1 for v in stamps.values()), 'authority clock'
    assert first['attempt']['worker'] == 'w1' and first['attempt']['handler'] == 'native.v1'
    assert first['attempt']['started_at'] <= stamps['reported_at']
    time.sleep(.05)
    client.request('worker/heartbeat', dict(base, progress=run))  # same document again
    again = caller.get(job['id'])['progress']
    assert again['changed_at'] == stamps['changed_at'] and again['phase_started_at'] == stamps['phase_started_at']
    assert again['reported_at'] > stamps['reported_at'], 'the heartbeat still carried progress'
    time.sleep(.05)
    client.request('worker/heartbeat', dict(base, progress=dict(run, done=38)))  # counts moved, phase same
    moved = caller.get(job['id'])['progress']
    assert moved['changed_at'] > stamps['changed_at'] and moved['phase_started_at'] == stamps['phase_started_at']
    time.sleep(.05)
    client.request('worker/heartbeat', dict(base, progress={'phase': 'publish'}))
    assert caller.get(job['id'])['progress']['phase_started_at'] > stamps['phase_started_at']
    client.request('worker/heartbeat', base)  # no progress: stamps stay
    assert caller.get(job['id'])['progress']['phase'] == 'publish'


@pytest.mark.parametrize('bad', [
    {'phase': 'run', 'done': 5, 'total': 4},
    {'phase': 'run', 'done': -1},
    {'phase': 'run', 'done': 1.5},
    {'phase': 'run', 'done': True},
    {'phase': 'run', 'total': 10**7 + 1},
    {'phase': 'run', 'unit': 'x'*25},
    {'phase': 'run', 'unit': 'two words'},
    {'phase': 'run', 'message': 'x'*201},
    {'phase': 'run', 'message': 'two\nlines'},
    {'phase': 'x'*65},
    {'phase': 'run', 'changed_at': 1},       # handlers never supply times
    {'phase': 'run', 'reported_at': 1},
    {'phase': 'run', 'secret': 'x'},
])
def test_v1_validation_refuses_and_stores_nothing(api, bad):
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    job = caller.submit(submit_spec('bad', digest))
    attempt = running_attempt(api, digest)
    with pytest.raises(Exception):
        client.request('worker/heartbeat', dict(boot='boot', attempt_id=attempt['attempt_id'],
                                                fence=attempt['fence'], progress=bad))
    assert 'progress' not in caller.get(job['id'])


def test_legacy_detail_is_an_alias_cut_to_one_short_line_and_message_is_stored_as_sent(api):
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    caller.submit(submit_spec('detail', digest))
    attempt = running_attempt(api, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    client.request('worker/heartbeat', dict(base, progress={'phase': 'run', 'detail': 'a\nb' + 'é'*500}))
    stored = document(caller.get(attempt['job_id']))['detail']
    assert '\n' not in stored and len(stored.encode()) <= 200 and stored.startswith('a b')
    client.request('worker/heartbeat', dict(base, progress={'phase': 'run', 'detail': '3/9 assertions'}))
    assert document(caller.get(attempt['job_id'])) == {'phase': 'run', 'detail': '3/9 assertions'}


def test_activity_is_stamped_by_the_authority_and_only_accepts_the_log_source(api):
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    job = caller.submit(submit_spec('activity', digest))
    attempt = running_attempt(api, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    assert 'activity_at' not in caller.get(job['id'])['attempt']
    for bad in ({'source': 'log', 'text': 'x'}, {'source': 'other'}, {}, 'log', {'at': 1}):
        with pytest.raises(Exception):
            client.request('worker/heartbeat', dict(base, activity=bad))
    client.request('worker/heartbeat', dict(base, activity={'source': 'log'}))
    read = caller.get(job['id'])
    assert read['attempt']['activity_at'] >= read['attempt']['started_at']
    assert 'progress' not in read, 'log activity claims no phase'


def test_old_worker_bodies_and_old_rows_still_work(api, tmp_path):
    """A pre-v1 row (progress stored, no stamps) reads back without invented times."""
    client, digest = api
    caller = WorkloadClient(client.url[:-len('v1/workloads/')], 'a'*32)
    job = caller.submit(submit_spec('old', digest))
    attempt = running_attempt(api, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    client.request('worker/heartbeat', dict(base, progress={'phase': 'old', 'fraction': .1}))
    import sqlite3
    db = sqlite3.connect(tmp_path/'jobs.db')
    db.execute('UPDATE attempts SET progress_reported=NULL,progress_changed=NULL,phase_started=NULL')
    db.commit()
    db.close()
    assert caller.get(job['id'])['progress'] == {'phase': 'old', 'fraction': .1, 'dropped': 0}


def test_handler_without_progress_json_shows_log_activity_and_no_text(executing, tmp_path):
    caller, config, digest = executing
    script = tmp_path/'noisy.py'
    script.write_text('''import os, time
log = os.path.join(os.environ['HARMONY_OUTPUT'], 'noisy.log')
for n in range(30):
    with open(log, 'a') as out:
        out.write('SECRET-LOOKING TEXT %d\\n' % n)
    time.sleep(.1)
''')
    config['handlers']['native.v1']['argv'] = [sys.executable, str(script)]
    job = caller.submit(submit_spec('noisy', digest))
    worker = WorkloadWorker(config)
    stepping = Thread(target=worker.step)
    stepping.start()
    seen = None
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline and stepping.is_alive():
            read = caller.get(job['id'])
            if read.get('attempt', {}).get('activity_at'):
                seen = read
                break
            time.sleep(.05)
        stepping.join(timeout=60)
    finally:
        worker.close()
    assert seen is not None, 'a log-writing handler is visible without any progress.json'
    assert 'progress' not in seen
    assert 'SECRET' not in json.dumps(seen), 'log text never leaves the worker'


def test_python_helper_writes_v1_atomically_and_keeps_the_latest_within_a_second(tmp_path, monkeypatch):
    from livestack_node.workloads import lease_helper
    monkeypatch.setenv('HARMONY_OUTPUT', str(tmp_path))
    monkeypatch.setattr(lease_helper, '_progress_state', {'last': 0.0, 'pending': None, 'timer': None})
    path = tmp_path/'progress.json'
    lease_helper.report_progress('run', done=1, total=3, unit='steps', message='first')
    assert json.loads(path.read_text()) == {'phase': 'run', 'done': 1, 'total': 3, 'unit': 'steps', 'message': 'first'}
    for n in range(2, 4):
        lease_helper.report_progress('run', done=n, total=3, unit='steps')
    assert json.loads(path.read_text())['done'] == 1, 'intermediates are held back inside the second'
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and json.loads(path.read_text())['done'] != 3:
        time.sleep(.05)
    assert json.loads(path.read_text()) == {'phase': 'run', 'done': 3, 'total': 3, 'unit': 'steps'}
    with pytest.raises(Exception):
        lease_helper.report_progress('run', done=9, total=3)


def test_node_helper_matches_the_python_contract(tmp_path):
    import shutil
    node = shutil.which('node')
    if node is None:
        pytest.skip('requires node')
    helper = Path(livestack_node.__file__).resolve().parent/'workloads/lease_helper.mjs'
    script = tmp_path/'handler.mjs'
    script.write_text(f'''import {{ reportProgress }} from {str(helper)!r};
import {{ readFileSync }} from 'node:fs';
const read = () => JSON.parse(readFileSync(process.env.HARMONY_OUTPUT + '/progress.json', 'utf8'));
reportProgress('run', {{ done: 1, total: 3, unit: 'steps', message: 'first' }});
console.log(JSON.stringify(read()));
reportProgress('run', {{ done: 2, total: 3 }});
reportProgress('run', {{ done: 3, total: 3 }});
console.log(JSON.stringify(read()));
for (const bad of [{{ done: 9, total: 3 }}, {{ message: 'a\\nb' }}, {{ message: 'x'.repeat(201) }}, {{ unit: 'two words' }}, {{ changed_at: 1 }}]) {{
  try {{ reportProgress('run', bad); console.log('ACCEPTED ' + JSON.stringify(bad)); }} catch {{ }}
}}
// exits with a held-back write: the exit hook must flush the latest
''')
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30,
                         env={'HARMONY_OUTPUT': str(tmp_path), 'PATH': '/usr/bin:/bin:' + str(Path(node).parent)})
    assert run.returncode == 0, run.stderr
    lines = run.stdout.strip().splitlines()
    assert json.loads(lines[0]) == {'phase': 'run', 'done': 1, 'total': 3, 'unit': 'steps', 'message': 'first'}
    assert json.loads(lines[1])['done'] == 1 and len(lines) == 2, lines
    assert json.loads((tmp_path/'progress.json').read_text()) == {'phase': 'run', 'done': 3, 'total': 3}


# ---- structural enforcement: contract, token bucket, liveness vs semantic, deadline modes ----------

class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def policy_api(tmp_path):
    """Build an authority with an `attempt_progress` policy and a clock the test moves."""
    started = []

    def build(policy):
        clock = Clock()
        store = WorkloadStore(tmp_path/f'jobs{len(started)}.db', handlers={'native.v1'}, clock=clock,
                              progress_policy=policy)
        server = WorkloadServer(('127.0.0.1', 0), store, [
            Principal('alice', 'a'*32, 'caller', ('native.v1',)),
            Principal('worker', 'w'*32, 'worker', worker='w1', host='host1')])
        data = b'input fixture'
        digest = hashlib.sha256(data).hexdigest()
        server.blobs.put('alice', digest, len(data), BytesIO(data))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        url = f'http://127.0.0.1:{server.server_port}'
        worker = WorkloadClient(url, 'w'*32)
        caller = WorkloadClient(url, 'a'*32)
        return clock, server, worker, caller, digest
    yield build
    for server, thread in started:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def claim(worker, digest):
    return running_attempt((worker, digest), digest)


CONTRACT = {'max_silence_seconds': 30, 'max_per_period': {'count': 2, 'seconds': 60}}


def test_token_bucket_coalesces_excess_reports_counts_them_and_publishes_the_latest_later(policy_api):
    clock, server, worker, caller, digest = policy_api({'handlers': {'native.v1': CONTRACT}})
    job = caller.submit(submit_spec('bucket', digest))
    attempt = claim(worker, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    for n in range(1, 6):
        worker.request('worker/heartbeat', dict(base, progress={'phase': 'run', 'done': n, 'total': 9}))
    read = caller.get(job['id'])['progress']
    assert read['done'] == 2, 'two tokens, two published'
    assert read['dropped'] == 3, 'three over budget, coalesced not refused'
    clock.now += 31  # one token returns (2 per 60 s)
    worker.request('worker/heartbeat', base)  # no new progress: the pending latest is published
    read = caller.get(job['id'])['progress']
    assert read['done'] == 5 and read['dropped'] == 3


def test_a_default_bucket_never_touches_the_workers_ten_second_cadence(policy_api):
    clock, server, worker, caller, digest = policy_api(None)
    job = caller.submit(submit_spec('slow', digest))
    attempt = claim(worker, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    for n in range(1, 40):
        clock.now += 10
        worker.request('worker/heartbeat', dict(base, progress={'phase': 'run', 'done': n, 'total': 99}))
    read = caller.get(job['id'])['progress']
    assert read['done'] == 39 and read['dropped'] == 0


def test_contract_derived_limit_reports_by_default_and_enforces_when_flipped_per_handler(policy_api):
    clock, server, worker, caller, digest = policy_api({'handlers': {'native.v1': CONTRACT}})
    job = caller.submit(submit_spec('silent', digest))
    attempt = claim(worker, digest)
    base = dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    worker.request('worker/heartbeat', dict(base, activity={'source': 'log'}))
    clock.now += 100
    worker.request('worker/heartbeat', dict(base, activity={'source': 'log'}))
    read = caller.get(job['id'])
    assert read['state'] == 'running', 'report mode never fails a job'
    verdict = read['progress_verdict']
    assert verdict['state'] == 'overdue' and verdict['mode'] == 'report' and verdict['source'] == 'contract'
    assert verdict['limit_seconds'] == 30 and verdict['quiet_seconds'] >= 100
    assert verdict['activity_quiet_seconds'] == 0, 'worker-owned liveness is separate from handler progress'
    assert 'progress' not in read and read['attempt']['activity_at']


def test_flipping_a_handler_to_enforce_fails_the_stalled_job_with_the_existing_cause(policy_api):
    clock, server, worker, caller, digest = policy_api(
        {'handlers': {'native.v1': dict(CONTRACT, deadline_mode='enforce')}})
    job = caller.submit(submit_spec('enforced', digest))
    attempt = claim(worker, digest)
    clock.now += 100
    read = caller.get(job['id'])
    assert read['state'] == 'failed' and read['cause']['kind'] == 'stalled_no_progress'


def test_spec_deadline_keeps_enforcing_by_default_and_reports_when_policy_says_report(policy_api):
    for policy, state, mode in (({}, 'failed', None), ({'deadline_mode': 'report'}, 'running', 'report')):
        clock, server, worker, caller, digest = policy_api(policy)
        job = caller.submit(dict(submit_spec('spec', digest), version=4, progress_deadline_seconds=60))
        claim(worker, digest)
        clock.now += 100
        read = caller.get(job['id'])
        assert read['state'] == state
        if mode:
            assert read['progress_verdict']['mode'] == mode and read['progress_verdict']['source'] == 'spec'


def test_a_handler_without_any_limit_gets_no_verdict(policy_api):
    clock, server, worker, caller, digest = policy_api(None)
    job = caller.submit(submit_spec('free', digest))
    attempt = claim(worker, digest)
    for _ in range(100):  # 10 000 s of silence, lease kept alive
        clock.now += 100
        worker.request('worker/heartbeat', dict(boot='boot', attempt_id=attempt['attempt_id'], fence=attempt['fence']))
    read = caller.get(job['id'])
    assert read['state'] == 'running' and 'progress_verdict' not in read


def test_manifest_contract_is_validated_and_strict_mode_refuses_a_release_without_one(policy_api):
    from livestack_node.workloads.handler_release import validate_manifest
    fixture = json.loads((Path(__file__).parent/'fixtures/harmony-handler-release-v1/manifest.json').read_text())
    for bad in ({'max_silence_seconds': 5}, {}, {'max_silence_seconds': 60, 'x': 1},
                {'max_silence_seconds': 60, 'max_per_period': {'count': 0, 'seconds': 5}}):
        with pytest.raises(WorkloadError, match='progress_contract'):
            validate_manifest(dict(fixture, progress_contract=bad))
    validate_manifest(dict(fixture, progress_contract=CONTRACT))
    release = validate_manifest(fixture)
    request = dict(manifest=fixture, release_digest=release['release_digest'], archive_digest='0'*64, archive_bytes=1)
    clock, server, worker, caller, digest = policy_api({'strict_contract': True})
    with pytest.raises(WorkloadError, match='handler_progress_contract_missing'):
        server.handler_registry.stage('actor', request)
    clock, server, worker, caller, digest = policy_api(None)
    with pytest.raises(WorkloadError, match='handler_release_policy_refused'):
        server.handler_registry.stage('actor', request)  # permissive: gets as far as ordinary policy


def test_policy_section_is_strict_about_keys_and_modes():
    from livestack_node.workloads.model import progress_policy
    assert progress_policy(None) == {'strict_contract': False, 'deadline_mode': 'enforce', 'handlers': {}}
    for bad in ({'strict': True}, {'deadline_mode': 'maybe'}, {'handlers': {'h': {'deadline_mode': 'x'}}},
                {'handlers': {'h': {'max_silence_seconds': 1}}}):
        with pytest.raises(WorkloadError):
            progress_policy(bad)
