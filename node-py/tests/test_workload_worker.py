"""Actual HTTP authority, private input transfer and systemd worker execution."""
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

import pytest

from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.supervision import SystemdExecutor
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.worker import WorkloadWorker


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    # These tests exercise worker execution and reconciliation, not placement
    # under the developer host's incidental load.
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux systemd user manager and cgroup v2')
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'native.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='integration', host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = tmp_path/'installed-handler.py'
    script.write_text('''import json,os,time
from pathlib import Path
request=json.loads(Path(os.environ['HARMONY_REQUEST']).read_text())
time.sleep(request.get('sleep',0))
value=Path('input').read_text()
if request.get('object'):
    value+='|'+Path(os.environ['HARMONY_INPUT_OBJECTS'],request['object']).read_text()
Path(os.environ['HARMONY_OUTPUT'],'artifact').write_text(value)
print('finished')
raise SystemExit(request.get('exit',0))
''')
    url = f'http://127.0.0.1:{server.server_port}'
    config = dict(authority=url, token='w'*32, worker='integration', state_dir=str(tmp_path/'state'),
        workspace=str(tmp_path/'workspace'), require_dedicated_filesystem=False, lease_interval=.2,
        capacity={'cpu':1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        memory_reserve_bytes=0,disk_reserve_bytes=0,environment={'PATH':'/usr/bin:/bin'},
        handlers={'native.v1':dict(argv=[sys.executable,str(script)],outputs=['artifact'])})
    caller = WorkloadClient(url, 'a'*32)
    source = tmp_path/'source'
    source.mkdir()
    (source/'input').write_text('captured bytes')
    capture(source, ['input'], tmp_path/'source.tar')
    digest = InputTransfer(caller).put(tmp_path/'source.tar')['digest']
    (source/'input').write_text('later edits must not enter execution')
    try:
        yield store, config, caller, digest
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def submit(caller, digest, **payload):
    return caller.submit(dict(version=1,key='one',handler='native.v1',input_digest=digest,
        need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},payload=payload))


@pytest.mark.parametrize('exit_code, expected', [(0,'succeeded'), (7,'failed')])
def test_worker_executes_pinned_input_and_returns_owned_artifact(fleet, tmp_path, exit_code, expected):
    store, config, caller, digest = fleet
    job = submit(caller, digest, exit=exit_code)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == expected
        detail = result['result']
        assert detail['result']['exit_code'] == exit_code
        refs = detail['result']['artifacts']
        artifact = next(r for r in refs if r['name'] == 'artifact')
        received = InputTransfer(caller).get(artifact['digest'], tmp_path/'returned')
        assert received.read_text() == 'captured bytes'
        assert worker.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        assert not worker.step()  # Product failure was not retried.
    finally:
        worker.close()


def test_worker_fetches_only_declared_supplemental_inputs(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    _, config, caller, digest = fleet
    extra = tmp_path/'component.bin'; extra.write_text('accepted component bytes')
    uploaded = InputTransfer(caller).put(extra)
    job = caller.submit(dict(version=2,key='multi',handler='native.v1',input_digest=digest,
        input_objects=[{'name':'components/component.bin',**uploaded}],
        need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        payload={'object':'components/component.bin'}))
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded'
        artifact = next(a for a in result['result']['result']['artifacts'] if a['name'] == 'artifact')
        returned = InputTransfer(caller).get(artifact['digest'], tmp_path/'multi-result')
        assert returned.read_text() == 'captured bytes|accepted component bytes'
        assert list(Path(config['workspace']).iterdir()) == []
    finally:
        worker.close()



def test_exit_between_receipt_read_and_unit_inspection_preserves_product_failure(fleet):
    # Only the observation ordering is injected: HTTP, SQLite, the systemd
    # process, and its atomically published receipt all remain real.
    _, config, caller, digest = fleet
    job = submit(caller, digest, exit=7, sleep=.3)
    worker = WorkloadWorker(config)
    original = worker.executor.exit_result
    injected = False
    def read_then_allow_exit(output):
        nonlocal injected
        result = original(output)
        if result is None and not injected:
            injected = True
            attempt = Path(output).parent.name
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                state = worker.executor.inspect(attempt)
                if state.get('ActiveState') in ('inactive', 'failed'):
                    break
                time.sleep(.02)
            else:
                raise AssertionError('real execution did not finish')
            assert original(output)['exit_code'] == 7
        return result
    worker.executor.exit_result = read_then_allow_exit
    try:
        assert worker.step()
        assert injected
        result = caller.get(job['id'])
        assert result['state'] == 'failed', result
        assert result['result']['outcome'] == 'product_failure'
        assert result['result']['result']['exit_code'] == 7
        assert len(result['attempts']) == 1
        assert not worker.step()
    finally:
        worker.close()


def test_restart_after_exit_receipt_replays_completion_without_new_attempt(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    _, config, caller, digest = fleet
    job = submit(caller, digest, exit=7)
    worker = WorkloadWorker(config)
    def interrupt_after_receipt(*_args):
        raise RuntimeError('simulated supervisor stop before artifact upload')

    worker._attach_artifacts = interrupt_after_receipt
    try:
        with pytest.raises(RuntimeError, match='simulated supervisor stop'):
            worker.step()
        journal = worker.journal.read()
        assert journal['phase'] == 'running'
        attempt = journal['assignment']['attempt_id']
        assert worker.executor.exit_result(Path(config['workspace'])/attempt/'output')['exit_code'] == 7
    finally:
        worker.close()

    recovered = WorkloadWorker(config)
    try:
        recovered.reconcile()
        result = caller.get(job['id'])
        assert result['state'] == 'failed'
        assert result['result']['outcome'] == 'product_failure'
        assert result['result']['result']['exit_code'] == 7
        assert len(result['attempts']) == 1
        artifact = next(a for a in result['result']['result']['artifacts'] if a['name'] == 'artifact')
        assert InputTransfer(caller).get(artifact['digest'], tmp_path/'recovered').read_text() == 'captured bytes'
        assert recovered.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
    finally:
        recovered.close()


def test_fenced_recovery_artifact_upload_finishes_cleanup(fleet):
    store, config, caller, digest = fleet
    job = submit(caller, digest, exit=7)
    worker = WorkloadWorker(config)
    worker._attach_artifacts = lambda *_args: (_ for _ in ()).throw(
        RuntimeError('simulated supervisor stop before artifact upload'))
    try:
        with pytest.raises(RuntimeError, match='simulated supervisor stop'):
            worker.step()
        journal = worker.journal.read()
        assert journal['phase'] == 'running'
        attempt = journal['assignment']['attempt_id']
        assert worker.executor.exit_result(Path(config['workspace'])/attempt/'output')['exit_code'] == 7
    finally:
        worker.close()

    caller.request('jobs/'+job['id']+'/cancel', {})
    recovered = WorkloadWorker(config)
    try:
        recovered.reconcile()
        assert caller.get(job['id'])['state'] == 'cancelled'
        assert recovered.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state!='ended'").fetchone()[0] == 0
    finally:
        recovered.close()

def test_task_exhaustion_is_infrastructure_with_a_kernel_receipt(fleet, tmp_path):
    _, config, caller, digest = fleet
    script = Path(config['handlers']['native.v1']['argv'][1])
    script.write_text('''import os,subprocess
from pathlib import Path
children=[]
code=0
try:
    for _ in range(32):
        children.append(subprocess.Popen(['sleep','10']))
except BlockingIOError:
    code=7
    Path(os.environ['HARMONY_OUTPUT'],'artifact').write_text('task budget reached')
finally:
    for child in children: child.terminate()
    for child in children: child.wait()
raise SystemExit(code)
''')
    config['handlers']['native.v1'].update(max_tasks=12, infrastructure_outputs=['artifact'])
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == 'queued' and result['reason'] == 'infrastructure retry'
        assert result['result']['outcome'] == 'infrastructure'
        detail = result['result']['result']
        assert detail['exit_code'] == 7 and detail['resources']['pids_max_events'] > 0
        artifact = next(a for a in detail['artifacts'] if a['name'] == 'artifact')
        assert InputTransfer(caller).get(artifact['digest'], tmp_path/'receipt').read_text() == 'task budget reached'
        assert worker.journal.read() is None
        caller.request('jobs/'+job['id']+'/cancel', {})
    finally:
        worker.close()


def test_worker_reuses_verified_source_without_a_second_download(fleet, tmp_path):
    store, config, caller, digest = fleet
    config['input_cache_bytes'] = 64*1024**2
    first = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step() and caller.get(first['id'])['state'] == 'succeeded'
        # Real HTTP authority remains live for claims/heartbeats/completion, but
        # its source file is unavailable: only a verified cache hit can run.
        second_spec = dict(caller.get(first['id'])['spec'], key='second')
        second = caller.submit(second_spec)
        (Path(store.path).parent/'objects'/digest).unlink()
        worker.close()
        worker = WorkloadWorker(config)  # Cache survives a supervisor restart.
        assert worker.step() and caller.get(second['id'])['state'] == 'succeeded'
        entries = json.loads((worker.input_cache.root/'index.json').read_text())
        assert len(entries) == 1
        assert sum(row['size'] for row in entries.values()) <= config['input_cache_bytes']
        assert worker.journal.read() is None
    finally:
        worker.close()


def test_worker_prefers_verified_input_mirror_and_falls_back_to_authority(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    store, config, caller, digest = fleet
    mirror = tmp_path/'mirror'; mirror.mkdir()
    authority_object = Path(store.path).parent/'objects'/digest
    (mirror/digest).write_bytes(authority_object.read_bytes())
    fetcher = tmp_path/'fetch-mirror.py'
    fetcher.write_text('''import pathlib,shutil,sys
source=pathlib.Path(sys.argv[1],sys.argv[2])
if not source.is_file(): raise SystemExit(75)
shutil.copyfile(source,sys.argv[3])
''')
    config.update(input_cache_bytes=128*1024**2,
        input_mirror={'argv':[sys.executable,str(fetcher),str(mirror)],'max_seconds':5})
    first = submit(caller, digest)
    authority_object.unlink()
    worker = WorkloadWorker(config)
    try:
        assert worker.step() and caller.get(first['id'])['state'] == 'succeeded'
        source = tmp_path/'fallback-source'; source.mkdir(); (source/'input').write_text('authority fallback')
        capture(source, ['input'], tmp_path/'fallback.tar')
        fallback = InputTransfer(caller).put(tmp_path/'fallback.tar')['digest']
        second = caller.submit(dict(first['spec'], key='mirror-fallback', input_digest=fallback))
        assert worker.step() and caller.get(second['id'])['state'] == 'succeeded'
        entries = json.loads((worker.input_cache.root/'index.json').read_text())
        assert len(entries) == 1 and next(iter(entries.values()))['digest'] == fallback
    finally:
        worker.close()


def test_worker_mirrors_outputs_by_digest_without_making_cache_availability_authoritative(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    _, config, caller, digest = fleet
    mirror = tmp_path/'output-mirror'; mirror.mkdir()
    uploader = tmp_path/'upload-mirror.py'
    uploader.write_text('''import pathlib,shutil,sys
digest,source=sys.argv[2:]
shutil.copyfile(source,pathlib.Path(sys.argv[1],digest))
''')
    config['output_mirror'] = {'argv':[sys.executable,str(uploader),str(mirror)],'max_seconds':5}
    config['transfer_timeout'] = 90
    first = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.client.timeout == 15 and worker.transfer.client.timeout == 90
        assert worker.step() and caller.get(first['id'])['state'] == 'succeeded'
        result = caller.get(first['id'])['result']['result']
        artifact = next(a for a in result['artifacts'] if a['name'] == 'artifact')
        mirrored = mirror/artifact['digest']
        assert mirrored.read_text() == 'captured bytes'
        uploader.write_text('raise SystemExit(75)\n')
        second = caller.submit(dict(first['spec'], key='mirror-cache-outage'))
        assert worker.step() and caller.get(second['id'])['state'] == 'succeeded'
    finally:
        worker.close()


@pytest.mark.parametrize('retain,retention,expected', [(False,86400,'succeeded'), (True,1e-9,'queued'), (False,None,'queued')])
def test_cache_pressure_respects_retained_inputs_and_disabled_deletion(fleet, tmp_path, retain, retention, expected):
    _, config, caller, digest = fleet
    config.update(input_cache_bytes=64*1024**2, input_cache_entries=1, input_cache_retention_seconds=retention)
    first = caller.submit(dict(version=1,key='retained',handler='native.v1',input_digest=digest,retain=retain,
        need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},payload={}))
    worker = WorkloadWorker(config)
    try:
        assert worker.step() and caller.get(first['id'])['state'] == 'succeeded'
        source = tmp_path/'new-input'
        source.mkdir(); (source/'input').write_text('new captured source')
        capture(source, ['input'], tmp_path/'new.tar')
        newer = InputTransfer(caller).put(tmp_path/'new.tar')['digest']
        second = caller.submit(dict(first['spec'],key='newer',input_digest=newer,retain=False))
        assert worker.step() and caller.get(second['id'])['state'] == expected
        entries = json.loads((worker.input_cache.root/'index.json').read_text())
        assert len(entries) == 1
        assert next(iter(entries.values()))['digest'] == (newer if expected == 'succeeded' else digest)
        if expected == 'queued':
            assert caller.get(second['id'])['result']['outcome'] == 'infrastructure'
            caller.request('jobs/'+second['id']+'/cancel', {})
    finally:
        worker.close()


def test_corrupted_cached_bytes_cannot_execute(fleet):
    _, config, caller, digest = fleet
    config['input_cache_bytes'] = 64*1024**2
    first = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step() and caller.get(first['id'])['state'] == 'succeeded'
        cache = worker.input_cache.root
        key = next(iter(json.loads((cache/'index.json').read_text())))
        # Keep the length intact, so a size-only cache check cannot catch this.
        with (cache/key).open('r+b') as stream:
            stream.write(b'corrupted')
        second = caller.submit(dict(first['spec'],key='corrupt-cache'))
        assert worker.step()
        result = caller.get(second['id'])
        assert result['state'] == 'queued' and result['result']['outcome'] == 'infrastructure'
        assert result['result']['result']['artifacts'] == []
        assert result['result']['result']['detail'] == 'cached source digest mismatch'
        caller.request('jobs/'+second['id']+'/cancel', {})
    finally:
        worker.close()


def test_cancel_running_job_reconciles_before_readvertising_capacity(fleet):
    store, config, caller, digest = fleet
    job = submit(caller, digest, sleep=120)
    worker = WorkloadWorker(config)
    errors = []
    def execute():
        try:
            worker.step()
        except Exception as error:
            errors.append(error)
    thread = Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic()+10
        while True:
            journal = worker.journal.read()
            if journal and journal['phase'] == 'running':
                break
            assert time.monotonic() < deadline
            time.sleep(.05)
        caller.request('jobs/'+job['id']+'/cancel', {})
        thread.join(timeout=15)
        assert not thread.is_alive() and errors == []
        assert caller.get(job['id'])['state'] == 'cancelled'
        assert not worker.step()
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state='cleanup'").fetchone()[0] == 0
    finally:
        if thread.is_alive():
            caller.request('jobs/'+job['id']+'/cancel', {})
            thread.join(timeout=15)
        worker.close()


def test_production_worker_refuses_unbounded_developer_filesystem(fleet):
    _, config, _, _ = fleet
    config['require_dedicated_filesystem'] = True
    worker = WorkloadWorker(config)
    try:
        with pytest.raises(WorkloadError, match='dedicated bounded filesystem'):
            worker.step()
    finally:
        worker.close()


def test_observe_only_reports_headroom_without_claiming_work(fleet):
    store, config, caller, digest = fleet
    config['observe_only'] = True
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert not worker.step()
        assert caller.get(job['id'])['state'] == 'queued'
        with store.transaction() as db:
            row = db.execute('SELECT ready,report FROM workers').fetchone()
            assert not row['ready'] and json.loads(row['report'])['available']['memory_bytes'] > 0
    finally:
        worker.close()


def test_installed_enrollment_probe_returns_actual_limits(fleet, tmp_path):
    _, config, caller, digest = fleet
    script = Path(__file__).parents[1]/'livestack_node/workloads/probe.py'
    config['handlers']['native.v1'].update(argv=[sys.executable,str(script)], outputs=['probe.json'])
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded'
        ref = next(r for r in result['result']['result']['artifacts'] if r['name'] == 'probe.json')
        path = InputTransfer(caller).get(ref['digest'],tmp_path/'probe-result.json')
        probe = json.loads(path.read_text())
        assert int(probe['memory_max']) == 128*1024**2
        quota, period = map(int, probe['cpu_max'].split())
        assert quota/period == .1
        assert len(probe['source_manifest_digest']) == 64
    finally:
        worker.close()


def test_killed_worker_expires_then_new_process_reconciles_journal(fleet, tmp_path):
    store, config, caller, digest = fleet
    store.limits = Limits(lease_seconds=2)
    job = submit(caller, digest, sleep=120)
    config_path = tmp_path/'worker.json'
    config_path.write_text(json.dumps(config))
    process = subprocess.Popen([sys.executable, '-c',
        'import json,sys; import livestack_node.workloads.worker as worker; '
        'worker.os.getloadavg=lambda:(0,0,0); '
        'w=worker.WorkloadWorker(json.load(open(sys.argv[1]))); w.step()', str(config_path)])
    executor = SystemdExecutor(config['worker'])
    attempt, worker = None, None
    try:
        deadline = time.monotonic()+15
        path = Path(config['state_dir'])/'active.json'
        while True:
            if path.exists():
                journal = json.loads(path.read_text())
                if journal['phase'] == 'running':
                    attempt = journal['assignment']['attempt_id']
                    group = executor.inspect(attempt).get('ControlGroup')
                    if group:
                        break
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        process.kill()
        process.wait(timeout=5)
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        deadline = time.monotonic()+10
        while cgroup.exists() and 'populated 1' in (cgroup/'cgroup.events').read_text():
            assert time.monotonic() < deadline
            time.sleep(.1)
        worker = WorkloadWorker(config)
        worker.reconcile()
        assert caller.get(job['id'])['state'] == 'queued'
        assert worker.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state!='ended'").fetchone()[0] == 0
        caller.request('jobs/'+job['id']+'/cancel', {})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if attempt:
            executor.stop(attempt)
        if worker:
            worker.close()


@pytest.mark.parametrize('exit_code, expected', [(0, 'succeeded'), (7, 'failed')])
def test_rootless_worker_delivers_pinned_artifact(fleet, tmp_path, exit_code, expected):
    import shutil
    if not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd')):
        pytest.skip('requires installed rootless Docker prerequisites')
    store, config, caller, digest = fleet
    config['handlers']['native.v1']['backend'] = 'rootless-docker'
    config['capacity']['memory_bytes'] = 512*1024**2
    job = caller.submit(dict(version=1, key='docker', handler='native.v1', input_digest=digest,
        need={'cpu': .5, 'memory_bytes': 512*1024**2, 'disk_bytes': 64*1024**2}, payload={'exit': exit_code}))
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        if result['state'] != expected:
            detail = result['result']
            logs = []
            for item in detail.get('result', {}).get('artifacts', []):
                logs.append(InputTransfer(caller).get(item['digest'], tmp_path/item['name']).read_text()[-3000:])
            pytest.fail(str(detail)+'\n'+'\n'.join(logs))
        artifact = next(r for r in result['result']['result']['artifacts'] if r['name'] == 'artifact')
        received = InputTransfer(caller).get(artifact['digest'], tmp_path/'docker-returned')
        assert received.read_text() == 'captured bytes'
        assert worker.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        assert not worker.step()
    finally:
        worker.close()


def test_infrastructure_failure_retains_log_artifact(fleet, tmp_path):
    _, config, caller, digest = fleet
    config['handlers']['native.v1'].update(
        argv=[sys.executable, '-c', 'print("preparation diagnostic"); raise SystemExit(75)'],
        infrastructure_exit_codes=[75])
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == 'queued'
        assert result['result']['outcome'] == 'infrastructure'
        refs = result['result']['result']['artifacts']
        log = next(item for item in refs if item['name'] == 'command.log')
        assert InputTransfer(caller).get(log['digest'], tmp_path/'diagnostic').read_text() == 'preparation diagnostic\n'
        assert worker.journal.read() is None
    finally:
        worker.close()
