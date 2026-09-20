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
    assert caller.get(job['id'])['progress'] == {'phase': 'tts', 'detail': 'synth'}
    client.request('worker/heartbeat', base)  # no progress: the last value stays
    assert caller.get(job['id'])['progress'] == {'phase': 'tts', 'detail': 'synth'}
    client.request('worker/heartbeat', dict(base, progress={'phase': 'stills', 'fraction': .5}))
    assert caller.get(job['id'])['progress'] == {'phase': 'stills', 'fraction': .5}
    # A heartbeat carrying malformed progress is refused, not stored.
    with pytest.raises(Exception):
        client.request('worker/heartbeat', dict(base, progress={'phase': 'x', 'fraction': 2}))
    assert caller.get(job['id'])['progress'] == {'phase': 'stills', 'fraction': .5}
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
    assert caller.get(job['id'])['progress'] == {'phase': 'stills'}
    assert caller.get(job['id'])['state'] == 'succeeded'
