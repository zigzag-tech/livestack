"""Actual HTTP authority, private input transfer and systemd worker execution."""
import json
import hashlib
import io
import os
import logging
import platform
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tarfile
from threading import Thread
import time

import pytest

from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.supervision import SystemdExecutor
from livestack_node.workloads.task_environments import TaskEnvironmentStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.worker import WorkloadWorker


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    # These tests exercise worker execution and reconciliation, not placement
    # under the developer host's incidental load.
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux systemd user manager and cgroup v2')
    authority_path = tmp_path/'authority/jobs.db'
    store = WorkloadStore(authority_path, handlers={'native.v1'}, environment_handlers={
        'native.v1': {'purpose': 'development', 'profile': 'native-test-v1'}})
    principals = [
        Principal('owner', 'a'*32, 'caller', ('native.v1',)),
        Principal('operator', 'o'*32, 'admin', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='integration', host='test-host'),
        Principal('worker-b', 'z'*32, 'worker', worker='integration-b', host='replacement-host')]
    release_policy = {'revision': 'worker-test-v1', 'retention_seconds': 24*60*60,
        'handlers': {'native.v1': {'runtime_ids': ['python3'], 'backends': ['native']}}}
    server = WorkloadServer(('127.0.0.1', 0), store, principals, handler_release_policy=release_policy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    authority_state = {'server': server, 'thread': thread}

    def restart_authority():
        previous = authority_state['server']
        previous_thread = authority_state['thread']
        previous.shutdown()
        previous_thread.join(timeout=5)
        previous.server_close()
        reopened = WorkloadStore(authority_path, handlers={'native.v1'}, environment_handlers={
            'native.v1': {'purpose': 'development', 'profile': 'native-test-v1'}})
        reopened.recover()
        replacement = WorkloadServer(('127.0.0.1', 0), reopened, principals,
                                     handler_release_policy=release_policy)
        replacement_thread = Thread(target=replacement.serve_forever, daemon=True)
        replacement_thread.start()
        authority_state.update(server=replacement, thread=replacement_thread)
        reopened._test_restart_authority = restart_authority
        return reopened, f'http://127.0.0.1:{replacement.server_port}'

    store._test_restart_authority = restart_authority
    script = tmp_path/'installed-handler.py'
    script.write_text('''import json,os,subprocess,sys,time
from pathlib import Path
request=json.loads(Path(os.environ['HARMONY_REQUEST']).read_text())
if request.get('cache_before_sleep'):
    cache=Path('build','cache.txt')
    cache.parent.mkdir(parents=True,exist_ok=True)
    cache.write_text('partial-cache')
if request.get('spawn_child'):
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    Path('build','child.pid').write_text(str(child.pid))
if request.get('allocate_mb'):
    block=bytearray(request['allocate_mb']*1024**2)
    for offset in range(0,len(block),4096): block[offset]=1
if request.get('write_mb'):
    with open('written.bin','wb') as written:
        for _ in range(request['write_mb']): written.write(os.urandom(1024**2))
        written.flush(); os.fsync(written.fileno())
if request.get('busy'):
    until=time.process_time()+request['busy']
    while time.process_time()<until: pass
time.sleep(request.get('sleep',0))
value=Path('input').read_text()
if request.get('assert_absent') and Path(request['assert_absent']).exists():
    raise SystemExit(71)
if request.get('assert_mode') is not None:
    if Path(request.get('mode_file','input')).stat().st_mode & 0o777 != request['assert_mode']:
        raise SystemExit(72)
if request.get('object'):
    value+='|'+Path(os.environ['HARMONY_INPUT_OBJECTS'],request['object']).read_text()
if request.get('cache'):
    cache=Path('build','cache.txt')
    previous=cache.read_text() if cache.exists() else 'fresh'
    cache.parent.mkdir(parents=True,exist_ok=True)
    cache.write_text(previous+'|'+value)
    value+='|cache='+previous
if request.get('inspect_cache'):
    value+='|env-cache='+os.environ.get('HARMONY_ENV_CACHE_COMPONENTS','missing')
timing_path=os.environ.get('HARMONY_PHASE_TIMINGS')
if timing_path:
    timing_version=request.get('timing_version',2)
    if timing_version==0:
        Path(timing_path).write_text('{invalid timing json')
    elif timing_version==1:
        timing={'version':1,'dependencies_seconds':0.125,'compile_seconds':0.5,'test_seconds':0.25}
    else:
        timing={'version':2,'phases':{
            'dependencies':{'seconds':0.125},
            'compile':{'seconds':None,'reason':'not_applicable'},
            'test':{'seconds':0.25}}}
    if timing_version!=0:
        Path(timing_path).write_text(json.dumps(timing))
Path(os.environ['HARMONY_OUTPUT'],'artifact').write_text(value)
print('finished')
raise SystemExit(request.get('exit',0))
''')
    url = f'http://127.0.0.1:{server.server_port}'
    config = dict(authority=url, token='w'*32, worker='integration', state_dir=str(tmp_path/'state'),
        workspace=str(tmp_path/'workspace'), require_dedicated_filesystem=False, lease_interval=.2,
        handler_runtimes={'python3': sys.executable},
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
        authority_state['server'].shutdown()
        authority_state['thread'].join(timeout=5)
        authority_state['server'].server_close()


def submit(caller, digest, _key='one', _need=None, **payload):
    return caller.submit(dict(version=1,key=_key,handler='native.v1',input_digest=digest,
        need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2, **(_need or {})},payload=payload))


def test_task_e2e_attempt_environment_disables_python_bytecode(tmp_path):
    worker = WorkloadWorker.__new__(WorkloadWorker)
    worker.config = {'environment': {'PATH': '/usr/bin', 'PYTHONDONTWRITEBYTECODE': '0'}}
    worker.windows = False
    assignment = {
        'owner': 'alice',
        'spec': {'input_digest': 'a'*64, 'labels': {}},
        'environment': {'handle': 'h'*32, 'generation': 1, 'profile': 'task-e2e-v1',
            'purpose': 'task_e2e'},
    }

    env = worker._attempt_env(assignment, tmp_path, tmp_path/'output', tmp_path/'objects', 'attempt')

    assert env['PYTHONDONTWRITEBYTECODE'] == '1'


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


def _handler_package(tmp_path, version, output, delay=0, exit_code=0, infrastructure_exit_codes=(),
                     infrastructure_outputs=()):
    source = ("import os, time\nfrom pathlib import Path\n"
              "out=Path(os.environ['HARMONY_OUTPUT'])\n"
              "(out/'worker.pid').write_text(str(os.getpid()))\n"
              f"time.sleep({delay!r})\n"
              f"(out/{output!r}).write_text({version!r})\n"
              f"raise SystemExit({exit_code})\n").encode()
    manifest = dict(format='harmony-handler-package.v1', handler_id='native.v1', release_version=version,
        execution_contract=1, payload_schema='benchday.handler.request.v1',
        result_schema='benchday.handler.result.v1', platform='linux',
        architecture={'x86_64':'x86_64','amd64':'x86_64','aarch64':'aarch64','arm64':'arm64'}[platform.machine().lower()],
        backend='native', runtime_id='python3', entrypoint='handler.py', arguments=[], outputs=[output],
        infrastructure_outputs=sorted(infrastructure_outputs),
        infrastructure_exit_codes=sorted(infrastructure_exit_codes),
        files=[dict(path='handler.py', mode=0o444, size=len(source), sha256=hashlib.sha256(source).hexdigest())])
    release = validate_manifest(manifest)
    archive_path = tmp_path/f'{version}.tar'
    with tarfile.open(archive_path, 'w', format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo('handler.py')
        info.mode, info.size, info.mtime, info.uid, info.gid = 0o444, len(source), 0, 0, 0
        info.uname = info.gname = ''
        archive.addfile(info, io.BytesIO(source))
    return dict(manifest=manifest, release_digest=release['release_digest'], archive_path=archive_path,
        archive_digest=hashlib.sha256(archive_path.read_bytes()).hexdigest(), archive_bytes=archive_path.stat().st_size)


def _stage_handler(client, package):
    uploaded = InputTransfer(client).put(package['archive_path'])
    assert uploaded['digest'] == package['archive_digest']
    client.request('handler-releases/stage', dict(manifest=package['manifest'],
        release_digest=package['release_digest'], archive_digest=uploaded['digest'], archive_bytes=uploaded['size']))


def _activate_handler(client, package, generation):
    import uuid
    return client.request('handler-releases/activate', dict(request_id=uuid.uuid4().hex,
        expected_generation=generation, handler_id='native.v1', release_digest=package['release_digest']))


def test_live_release_activation_keeps_running_attempt_pinned_and_worker_alive(fleet, tmp_path):
    store, config, caller, digest = fleet
    operator = WorkloadClient(config['authority'], 'o'*32)
    first = _handler_package(tmp_path, 'release-a', 'a.out', delay=12)
    second = _handler_package(tmp_path, 'release-b', 'b.out')
    worker = WorkloadWorker(config)
    thread = None
    try:
        _stage_handler(operator, first)
        _activate_handler(operator, first, 0)
        first_job = caller.submit(dict(version=1, key='live-a', handler='native.v1', input_digest=digest,
            need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2}, payload={},
            handler_release={'selection':'default'}))
        worker.config['status_report_seconds'] = .2
        outcome = {}
        thread = Thread(target=lambda: outcome.update(worked=worker.step()), daemon=True)
        boot = worker.boot
        thread.start()
        deadline = time.monotonic()+10
        pid_path = None
        while time.monotonic() < deadline:
            journal = worker.journal.read()
            if journal:
                pid_path = Path(config['workspace'])/journal['assignment']['attempt_id']/'output'/'worker.pid'
                if pid_path.exists():
                    break
            time.sleep(.05)
        assert pid_path and pid_path.exists(), 'release A started under the real supervisor'
        pid = int(pid_path.read_text())
        before_start = Path(f'/proc/{pid}/stat').read_text().split()[21]

        _stage_handler(operator, second)
        _activate_handler(operator, second, 1)
        deadline = time.monotonic()+6
        while time.monotonic() < deadline and worker.handler_registry_state['generation'] < 2:
            time.sleep(.05)
        assert worker.handler_registry_state['generation'] == 2, 'the live worker installed B without a restart'
        assert worker.boot == boot
        assert Path(f'/proc/{pid}/stat').read_text().split()[21] == before_start
        os.kill(pid, 0)

        thread.join(timeout=15)
        assert not thread.is_alive()
        assert outcome.get('worked') is True
        result_a = caller.get(first_job['id'])
        assert result_a['state'] == 'succeeded'
        assert result_a['result']['handler_result_identity']['handler_release']['release_digest'] == first['release_digest']
        artifact_a = next(item for item in result_a['result']['result']['artifacts'] if item['name'] == 'a.out')
        assert InputTransfer(caller).get(artifact_a['digest'], tmp_path/'a.out').read_text() == 'release-a'

        second_job = caller.submit(dict(version=1, key='live-b', handler='native.v1', input_digest=digest,
            need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2}, payload={},
            handler_release={'selection':'default'}))
        assert worker.step() is True
        result_b = caller.get(second_job['id'])
        assert result_b['state'] == 'succeeded'
        assert result_b['result']['handler_result_identity']['handler_release']['release_digest'] == second['release_digest']
        artifact_b = next(item for item in result_b['result']['result']['artifacts'] if item['name'] == 'b.out')
        assert InputTransfer(caller).get(artifact_b['digest'], tmp_path/'b.out').read_text() == 'release-b'
    finally:
        if thread and thread.is_alive():
            thread.join(timeout=10)
        worker.close()
        operator.close()


def test_restart_recovery_uses_pinned_release_policy_and_outputs(fleet, tmp_path):
    _store, config, caller, digest = fleet
    operator = WorkloadClient(config['authority'], 'o'*32)
    first = _handler_package(tmp_path, 'recovery-a', 'a.out', exit_code=75,
        infrastructure_exit_codes=(75,), infrastructure_outputs=('a.out',))
    second = _handler_package(tmp_path, 'recovery-b', 'b.out', exit_code=75)
    worker = recovered = None
    try:
        _stage_handler(operator, first)
        _activate_handler(operator, first, 0)
        job = caller.submit(dict(version=1, key='recovery-a', handler='native.v1', input_digest=digest,
            need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2}, payload={},
            handler_release={'selection':'default'}))

        worker = WorkloadWorker(config)
        def interrupt_after_exit_receipt(*_args):
            raise RuntimeError('simulated worker restart before result upload')

        worker._attach_artifacts = interrupt_after_exit_receipt
        with pytest.raises(RuntimeError, match='simulated worker restart'):
            worker.step()
        journal = worker.journal.read()
        assert journal['phase'] == 'running'
        assert journal['assignment']['handler_release']['release_digest'] == first['release_digest']
        attempt = journal['assignment']['attempt_id']
        output = Path(config['workspace'])/attempt/'output'
        assert worker.executor.exit_result(output)['exit_code'] == 75
        assert (output/'a.out').read_text() == 'recovery-a'
        worker.close()
        worker = None

        _stage_handler(operator, second)
        _activate_handler(operator, second, 1)
        recovered = WorkloadWorker(config)
        recovered.reconcile()

        result = caller.get(job['id'])
        assert result['state'] == 'queued'
        assert result['result']['outcome'] == 'infrastructure'
        identity = result['result']['handler_result_identity']
        assert identity['handler_release']['release_digest'] == first['release_digest']
        artifacts = result['result']['result']['artifacts']
        assert [artifact['name'] for artifact in artifacts if artifact['name'].endswith('.out')] == ['a.out']
        artifact = next(item for item in artifacts if item['name'] == 'a.out')
        assert InputTransfer(caller).get(artifact['digest'], tmp_path/'recovered-a.out').read_text() == 'recovery-a'
        assert len(result['attempts']) == 1
        assert recovered.journal.read() is None
    finally:
        if worker is not None:
            worker.close()
        if recovered is not None:
            recovered.close()
        operator.close()


def test_release_specific_exit_policy_uses_the_pinned_descriptor(fleet, tmp_path):
    _store, config, _caller, _digest = fleet
    first = _handler_package(tmp_path, 'exit-a', 'a.out', exit_code=75,
                             infrastructure_exit_codes=(75,))
    second = _handler_package(tmp_path, 'exit-b', 'b.out', exit_code=75)
    worker = WorkloadWorker(config)
    try:
        for package in (first, second):
            descriptor = dict(manifest=package['manifest'], release_digest=package['release_digest'],
                archive_digest=package['archive_digest'], archive_bytes=package['archive_bytes'])
            worker.handler_packages.install(package['archive_path'], descriptor)

        def classify(package):
            manifest = package['manifest']
            identity = dict(handler_id=manifest['handler_id'], release_digest=package['release_digest'],
                execution_contract=manifest['execution_contract'], payload_schema=manifest['payload_schema'],
                result_schema=manifest['result_schema'])
            assignment = dict(spec={'handler': manifest['handler_id']}, handler_release=identity)
            return worker._completion_from_exit(assignment, {'exit_code': 75, 'resources': {}})['outcome']

        # The supervised live A/B integration covers process, lease and artifact
        # pinning. This isolates the scalar exit-policy difference itself.
        assert classify(first) == 'infrastructure'
        assert classify(second) == 'product_failure'
    finally:
        worker.close()


def test_handler_output_bound_allows_eight_gib_without_raising_input_bound(
        fleet, tmp_path, monkeypatch):
    _, config, _caller, digest = fleet
    config['handlers']['native.v1']['output_max_bytes'] = 8 * 1024**3
    worker = WorkloadWorker(config)
    output = tmp_path/'output'
    output.mkdir()
    artifact_path = output/'artifact'
    with artifact_path.open('wb') as artifact_file:
        artifact_file.truncate(2 * 1024**3 + 1)

    try:
        assert worker.transfer.max_bytes == 2 * 1024**3
        assert worker.output_transfers['native.v1'].max_bytes == 8 * 1024**3
        with pytest.raises(WorkloadError, match='upload byte limit exceeded'):
            worker.transfer.put(artifact_path)

        artifact_digest = 'b' * 64
        monkeypatch.setattr('livestack_node.workloads.transfer.file_digest',
                            lambda _path: artifact_digest)

        def acknowledge_upload(_target, method, _path, *, headers, body, timeout):
            assert method == 'PUT'
            assert body.seek(0, 2) == int(headers['Content-Length'])
            return 200, {}, json.dumps({
                'digest': artifact_digest,
                'size': int(headers['Content-Length']),
            }).encode()

        monkeypatch.setattr('livestack_node.workloads.transfer.transport.dial',
                            acknowledge_upload)
        assignment = {
            'spec': {'handler': 'native.v1', 'input_digest': digest},
            'boot': worker.boot,
            'attempt_id': 'a' * 32,
            'fence': 1,
        }
        completion = {'outcome': 'succeeded', 'result': {}}
        worker._attach_artifacts(assignment, completion, output)
        assert completion['result']['artifacts'] == [{
            'name': 'artifact', 'digest': artifact_digest,
            'size': 2 * 1024**3 + 1,
        }]
    finally:
        worker.close()


@pytest.mark.parametrize('invalid_limit', [True, 8 * 1024**3 + 1])
def test_worker_rejects_invalid_handler_output_bound(fleet, invalid_limit):
    _, config, _caller, _digest = fleet
    config['handlers']['native.v1']['output_max_bytes'] = invalid_limit
    with pytest.raises(WorkloadError, match='handler output_max_bytes'):
        WorkloadWorker(config)


def test_oversized_result_artifact_ends_the_attempt_naming_size_and_limit(fleet, tmp_path):
    # Real worker, systemd executor, authority and transfer: only the bound is
    # lowered. Before the fix the upload raised on every step (and again on every
    # reconcile) until the lease expired and the attempt was abandoned unexplained.
    store, config, caller, digest = fleet
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    worker.output_transfers['native.v1'] = InputTransfer(worker.transfer.client, max_bytes=4)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] != 'running'
        assert worker.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        detail = result['result']
        assert detail['outcome'] == 'infrastructure'
        body = detail['result']
        assert body['error'] == 'ArtifactTooLarge'
        assert body['artifact_bytes'] == len('captured bytes')
        assert body['limit_bytes'] == 4
        assert 'upload byte limit exceeded' in body['detail'] and '14 bytes' in body['detail']
        assert all(a['name'] != 'artifact' for a in body.get('artifacts', []))
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


def test_worker_reuses_task_environment_across_captured_source_edits(fleet, tmp_path, monkeypatch):
    store, config, caller, _first_digest = fleet
    environment_root = tmp_path/'task-environments'
    environment_root.mkdir()
    replacement_environment_root = tmp_path/'replacement-task-environments'
    replacement_environment_root.mkdir()
    config['task_environments'] = dict(root=str(environment_root), host_id='test-host',
        quota_helper='/unused/in-tests', project_id_min=100000, project_id_max=100063,
        max_bytes_per_replica=32*1024**3, max_bytes_per_owner=128*1024**3,
        max_total_bytes=256*1024**3, reserve_bytes=0, profiles={'native-test-v1': {
            'handlers': ['native.v1'], 'purpose': 'development', 'cache_contract': 'native-test-v1',
            'probe_argv': [sys.executable, '-c', 'print("native-test-toolchain-v1")'],
            'cache_components': [{'name': 'incremental-build', 'path': 'source/build',
                'inputs': [], 'contract': 'incremental-build-v1'}]}})

    def quota_usage_for(root):
        def quota_usage(project_ids):
            result = []
            for directory in root.iterdir():
                marker_path = directory/'environment.json'
                if not marker_path.is_file():
                    continue
                marker = json.loads(marker_path.read_text())
                project_id = marker['project_id']
                if project_id not in project_ids:
                    continue
                used = sum(path.lstat().st_blocks*512 for path in directory.rglob('*') if not path.is_dir())
                result.append({'project_id': project_id, 'used_bytes': used,
                    'hard_bytes': ((marker['quota_bytes']+1023)//1024)*1024})
            return result
        return quota_usage

    original_init = TaskEnvironmentStore.__init__
    def test_store_init(self, environment_config, **kwargs):
        return original_init(self, environment_config, **kwargs,
            quota_ensure=lambda handle, project, quota: {'quota_bytes': quota},
            quota_probe=lambda _: True, quota_usage=quota_usage_for(Path(environment_config['root'])),
            require_separate_filesystem=False, filesystem_bytes=8*1024**3)
    monkeypatch.setattr(TaskEnvironmentStore, '__init__', test_store_init)

    def submit_environment(job_key, digest, timing_version=2, **extra_payload):
        return caller.submit(dict(version=3, key=job_key, handler='native.v1', input_digest=digest,
            need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
            environment={'key':'test-task', 'reuse':'prefer'},
            payload={'cache':True, 'inspect_cache':True, 'timing_version':timing_version,
                **extra_payload}))

    def capture_revision(name, files, modes=None):
        source = tmp_path/name
        source.mkdir()
        for relative, contents in files.items():
            path = source/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            path.chmod((modes or {}).get(relative, 0o644))
        archive = tmp_path/f'{name}.tar'
        capture(source, sorted(files), archive)
        return InputTransfer(caller).put(archive)['digest']

    worker = WorkloadWorker(config)
    try:
        first_digest = capture_revision('environment-revision-one', {
            'input': b'captured bytes', 'obsolete.txt': b'old captured file'})
        first = submit_environment('environment-first', first_digest)
        assert worker.step()
        first_result = caller.get(first['id'])
        receipt = first_result['result']['environment_receipt']
        assert first_result['state'] == 'succeeded'
        assert receipt['reuse_outcome'] == 'created' and receipt['reason_code'] == 'created'
        assert receipt['phase_timings']['dependencies'] == {'seconds': 0.125}
        assert receipt['phase_timings']['compile'] == {
            'seconds': None, 'reason': 'not_applicable'}
        assert receipt['phase_timings']['test'] == {'seconds': 0.25}
        handle = first['environment_handle']
        cache = environment_root/handle/'source'/'build'/'cache.txt'
        assert cache.read_text() == 'fresh|captured bytes'
        assert (environment_root/handle/'source'/'obsolete.txt').read_bytes() == b'old captured file'

        second_digest = capture_revision('environment-revision-two',
            {'input': b'edited captured bytes'}, {'input': 0o755})
        second = submit_environment('environment-second', second_digest, timing_version=1,
            assert_absent='obsolete.txt', mode_file='input', assert_mode=0o755)
        assert second['environment_handle'] == handle
        assert worker.step()
        second_result = caller.get(second['id'])
        receipt = second_result['result']['environment_receipt']
        assert second_result['state'] == 'succeeded'
        assert receipt['reuse_outcome'] == 'reused'
        assert receipt['reason_code'] == 'source_updated_incrementally'
        assert receipt['phase_timings']['dependencies'] == {'seconds': 0.125}
        assert receipt['phase_timings']['compile'] == {'seconds': 0.5}
        assert receipt['phase_timings']['test'] == {'seconds': 0.25}
        assert receipt['cache_components'][0]['outcome'] == 'reused'
        assert cache.read_text() == 'fresh|captured bytes|edited captured bytes'
        artifact = next(item for item in second_result['result']['result']['artifacts']
                        if item['name'] == 'artifact')
        returned = InputTransfer(caller).get(artifact['digest'], tmp_path/'environment-artifact')
        text = returned.read_text()
        assert text.startswith('edited captured bytes|cache=fresh|captured bytes|env-cache=')
        cache_components = json.loads(text.split('|env-cache=', 1)[1])
        assert cache_components == [{
            'name': 'incremental-build', 'path': 'build', 'identity': receipt['cache_components'][0]['identity'],
            'outcome': 'reused'}]

        # Let a real worker on a second authority host reconstruct generation 3.
        # When the first worker reports again, it must reclaim only generation 2.
        replacement_config = dict(config, token='z'*32, worker='integration-b',
            state_dir=str(tmp_path/'replacement-state'), workspace=str(tmp_path/'replacement-workspace'),
            task_environments=dict(config['task_environments'], root=str(replacement_environment_root),
                                   host_id='replacement-host'))
        third_digest = capture_revision('environment-revision-three', {'input': b'relocated source'})
        third = submit_environment('environment-relocated', third_digest)
        assert third['environment_handle'] == handle
        with store.connect() as db:
            parked = db.execute('SELECT generation,state FROM task_environments WHERE handle=?',
                                 (handle,)).fetchone()
            prior = db.execute('SELECT host,generation,state FROM task_environment_replicas WHERE handle=?',
                               (handle,)).fetchall()
        assert tuple(parked) == (2, 'parked')
        assert [(row['host'], row['generation'], row['state']) for row in prior] == [
            ('test-host', 2, 'parked')]
        with store.transaction() as db:
            db.execute("UPDATE workers SET seen=0 WHERE id='integration'")
        worker_b = WorkloadWorker(replacement_config)
        assert worker_b.step()
        relocated = caller.get(third['id'])
        assert relocated['state'] == 'succeeded'
        assert relocated['result']['environment_receipt']['reuse_outcome'] == 'relocated'
        assert json.loads((replacement_environment_root/handle/'environment.json').read_text())['generation'] == 3

        cleanup = worker.register()
        assert cleanup['environment_cleanup'] == [{'handle': handle, 'generation': 2}]
        assert not (environment_root/handle).exists()
        with store.connect() as db:
            replicas = db.execute('SELECT host,generation FROM task_environment_replicas WHERE handle=?',
                                  (handle,)).fetchall()
        assert [(row['host'], row['generation']) for row in replicas] == [('replacement-host', 3)]

        malformed = submit_environment('environment-malformed-timing', second_digest, timing_version=0)
        assert worker_b.step()
        malformed_result = caller.get(malformed['id'])
        malformed_receipt = malformed_result['result']['environment_receipt']
        assert malformed_result['state'] == 'succeeded'
        assert all(malformed_receipt['phase_timings'][phase] == {
            'seconds': None, 'reason': 'handler_uninstrumented'}
            for phase in ('dependencies', 'compile', 'test'))
    finally:
        worker.close()
        if 'worker_b' in locals():
            worker_b.close()



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


def test_executor_exit_race_preserves_the_durable_receipt():
    class ExitRaceExecutor:
        def __init__(self):
            self.reads = 0

        def exit_result(self, _output):
            self.reads += 1
            return None if self.reads == 1 else {'exit_code': 0}

        def alive(self, _attempt):
            return False

    worker = object.__new__(WorkloadWorker)
    worker.executor = ExitRaceExecutor()
    assert worker._execution_live_or_complete('a'*32, Path('/unused'))
    assert worker.executor.reads == 2


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
        # Breaching the handler's own task limit ends the job (no retry into
        # the same cap) and names the fix; it is still not a product failure.
        assert result['state'] == 'failed' and result['reason'].startswith('resource limit: the attempt reached its task limit')
        assert result['result']['outcome'] == 'infrastructure' and len(result['attempts']) == 1
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


def test_worker_prefers_verified_input_mirror_and_falls_back_to_authority(fleet, tmp_path, monkeypatch, caplog):
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
        assert any('input mirror miss' in record.getMessage() and fallback[:12] in record.getMessage()
                   for record in caplog.records), 'a mirror miss must name its fallback'
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


def test_cancel_running_job_reconciles_before_readvertising_capacity(fleet, tmp_path, monkeypatch):
    store, config, caller, digest = fleet
    environment_root = tmp_path/'cancelled-task-environments'
    environment_root.mkdir()
    config['task_environments'] = dict(root=str(environment_root), host_id='test-host',
        quota_helper='/unused/in-tests', project_id_min=100000, project_id_max=100063,
        max_bytes_per_replica=32*1024**3, max_bytes_per_owner=128*1024**3,
        max_total_bytes=256*1024**3, reserve_bytes=0, profiles={'native-test-v1': {
            'handlers': ['native.v1'], 'purpose': 'development', 'cache_contract': 'native-test-v1',
            'probe_argv': [sys.executable, '-c', 'print("native-test-toolchain-v1")'],
            'cache_components': [{'name': 'incremental-build', 'path': 'source/build',
                'inputs': [], 'contract': 'incremental-build-v1'}]}})

    def quota_usage(project_ids):
        return [{'project_id': project_id, 'used_bytes': 0, 'hard_bytes': 32*1024**3}
                for project_id in project_ids]

    original_init = TaskEnvironmentStore.__init__
    def test_store_init(self, environment_config, **kwargs):
        return original_init(self, environment_config, **kwargs,
            quota_ensure=lambda _handle, _project, quota: {'quota_bytes': quota},
            quota_probe=lambda _root: True, quota_usage=quota_usage,
            require_separate_filesystem=False, filesystem_bytes=8*1024**3)
    monkeypatch.setattr(TaskEnvironmentStore, '__init__', test_store_init)

    job = caller.submit(dict(version=3, key='cancelled-task-environment', handler='native.v1',
        input_digest=digest, need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        environment={'key':'cancelled-task','reuse':'prefer'},
        payload={'sleep':120,'cache_before_sleep':True,'spawn_child':True}))
    handle = job['environment_handle']
    child_pid_file = environment_root/handle/'source'/'build'/'child.pid'
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
            if journal and journal['phase'] == 'running' and child_pid_file.exists():
                break
            assert time.monotonic() < deadline
            time.sleep(.05)
        attempt = journal['assignment']['attempt_id']
        group = worker.executor.inspect(attempt).get('ControlGroup')
        assert group
        caller.request('jobs/'+job['id']+'/cancel', {})
        thread.join(timeout=15)
        assert not thread.is_alive() and errors == []
        assert caller.get(job['id'])['state'] == 'cancelled'
        assert not worker.step()
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        assert not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text()
        marker = json.loads((environment_root/handle/'environment.json').read_text())
        assert marker['state'] == 'rebuild_required'
        assert marker['compatibility'] == '0'*64
        assert not any(replica['handle'] == handle for replica in worker.task_environments.report()[1])
        assert caller.get_environment(handle)['state'] == 'rebuild_required'
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state='cleanup'").fetchone()[0] == 0
    finally:
        if thread.is_alive():
            caller.request('jobs/'+job['id']+'/cancel', {})
            thread.join(timeout=15)
        worker.close()


def test_scope_close_cancels_task_environment_descendants_after_cleanup(fleet, tmp_path, monkeypatch):
    store, config, caller, digest = fleet
    environment_root = tmp_path/'cancelled-scope-task-environments'
    environment_root.mkdir()
    config['task_environments'] = dict(root=str(environment_root), host_id='test-host',
        quota_helper='/unused/in-tests', project_id_min=100000, project_id_max=100063,
        max_bytes_per_replica=32*1024**3, max_bytes_per_owner=128*1024**3,
        max_total_bytes=256*1024**3, reserve_bytes=0, profiles={'native-test-v1': {
            'handlers': ['native.v1'], 'purpose': 'development', 'cache_contract': 'native-test-v1',
            'probe_argv': [sys.executable, '-c', 'print("native-test-toolchain-v1")'],
            'cache_components': [{'name': 'incremental-build', 'path': 'source/build',
                'inputs': [], 'contract': 'incremental-build-v1'}]}})

    def quota_usage(project_ids):
        rows = []
        for marker_path in environment_root.glob('*/environment.json'):
            marker = json.loads(marker_path.read_text())
            if marker['project_id'] in project_ids:
                rows.append({'project_id': marker['project_id'], 'used_bytes': 0,
                    'hard_bytes': ((marker['quota_bytes']+1023)//1024)*1024})
        return rows

    original_init = TaskEnvironmentStore.__init__
    def test_store_init(self, environment_config, **kwargs):
        return original_init(self, environment_config, **kwargs,
            quota_ensure=lambda _handle, _project, quota: {'quota_bytes': quota},
            quota_probe=lambda _root: True, quota_usage=quota_usage,
            require_separate_filesystem=False, filesystem_bytes=8*1024**3)
    monkeypatch.setattr(TaskEnvironmentStore, '__init__', test_store_init)

    scope = {'key': 'cancelled-task-scope', 'lease_seconds': 300}
    environment = {'key': 'cancelled-task-environment', 'reuse': 'prefer'}
    running = caller.submit(dict(version=4, key='running-task-environment', handler='native.v1',
        input_digest=digest, need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        scope=scope, environment=environment,
        payload={'sleep':120,'cache_before_sleep':True,'spawn_child':True}))
    handle = running['environment_handle']
    child_pid_file = environment_root/handle/'source'/'build'/'child.pid'
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
            if journal and journal['phase'] == 'running' and child_pid_file.exists():
                break
            assert time.monotonic() < deadline
            time.sleep(.05)
        attempt = journal['assignment']['attempt_id']
        group = worker.executor.inspect(attempt).get('ControlGroup')
        assert group

        queued = caller.submit(dict(version=4, key='queued-task-environment', handler='native.v1',
            input_digest=digest, need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
            scope=scope, environment=environment, payload={'cache_before_sleep':True}))
        assert queued['environment_handle'] == handle
        closed = caller.close_scope(scope['key'], reason='cancel task group')
        assert (closed['cancelled'], closed['running_cleanup'], closed['replayed']) == (2, 1, False)
        thread.join(timeout=15)
        assert not thread.is_alive() and errors == []
        assert caller.get(running['id'])['state'] == 'cancelled'
        assert caller.get(queued['id'])['state'] == 'cancelled'
        assert caller.get_scope(scope['key'])['state'] == 'closed'
        assert not worker.step()

        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        assert not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text()
        marker = json.loads((environment_root/handle/'environment.json').read_text())
        assert marker['state'] == 'rebuild_required'
        assert caller.get_environment(handle)['state'] == 'rebuild_required'
        assert not any(replica['handle'] == handle for replica in worker.task_environments.report()[1])
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state='cleanup'").fetchone()[0] == 0
    finally:
        if thread.is_alive():
            caller.close_scope(scope['key'], reason='test cleanup')
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


def test_worker_and_authority_restart_rebuild_unconfirmed_task_environment(fleet, tmp_path, monkeypatch):
    """A killed attempt cannot leave its environment advertised as warm across either restart."""
    store, config, caller, digest = fleet
    store.limits = Limits(lease_seconds=2)
    environment_root = tmp_path/'retained-environments'
    environment_root.mkdir()
    config['task_environments'] = dict(root=str(environment_root), host_id='test-host',
        quota_helper='/unused/in-tests', project_id_min=100000, project_id_max=100063,
        max_bytes_per_replica=32*1024**3, max_bytes_per_owner=128*1024**3,
        max_total_bytes=256*1024**3, reserve_bytes=0, profiles={'native-test-v1': {
            'handlers': ['native.v1'], 'purpose': 'development', 'cache_contract': 'native-test-v1',
            'probe_argv': [sys.executable, '-c', 'print("native-test-toolchain-v1")'],
            'cache_components': [{'name': 'incremental-build', 'path': 'source/build',
                'inputs': [], 'contract': 'incremental-build-v1'}]}})

    def quota_usage(project_ids):
        rows = []
        for marker_path in environment_root.glob('*/environment.json'):
            marker = json.loads(marker_path.read_text())
            if marker['project_id'] in project_ids:
                rows.append({'project_id': marker['project_id'], 'used_bytes': 0,
                    'hard_bytes': ((marker['quota_bytes']+1023)//1024)*1024})
        return rows

    original_init = TaskEnvironmentStore.__init__
    def test_store_init(self, environment_config, **kwargs):
        return original_init(self, environment_config, **kwargs,
            quota_ensure=lambda handle, project, quota: {'quota_bytes': quota},
            quota_probe=lambda _: True, quota_usage=quota_usage,
            require_separate_filesystem=False, filesystem_bytes=8*1024**3)
    monkeypatch.setattr(TaskEnvironmentStore, '__init__', test_store_init)

    first = caller.submit(dict(version=3, key='crash-recovery-first', handler='native.v1',
        input_digest=digest, need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        environment={'key':'crash-recovery-task','reuse':'prefer'},
        payload={'sleep':120,'cache_before_sleep':True,'cache':True,'inspect_cache':True}))
    config_path = tmp_path/'task-environment-worker.json'
    config_path.write_text(json.dumps(config))
    runner = tmp_path/'run-task-environment-worker.py'
    runner.write_text('''import json,sys
from pathlib import Path
from livestack_node.workloads.task_environments import TaskEnvironmentStore
import livestack_node.workloads.worker as worker_module
root = Path(sys.argv[2])
original = TaskEnvironmentStore.__init__
def usage(project_ids):
    rows = []
    for path in root.glob('*/environment.json'):
        marker = json.loads(path.read_text())
        if marker['project_id'] in project_ids:
            rows.append({'project_id': marker['project_id'], 'used_bytes': 0,
                'hard_bytes': ((marker['quota_bytes']+1023)//1024)*1024})
    return rows
def test_init(self, config, **kwargs):
    return original(self, config, **kwargs,
        quota_ensure=lambda handle, project, quota: {'quota_bytes': quota},
        quota_probe=lambda _: True, quota_usage=usage,
        require_separate_filesystem=False, filesystem_bytes=8*1024**3)
TaskEnvironmentStore.__init__ = test_init
worker_module.os.getloadavg = lambda: (0,0,0)
worker = worker_module.WorkloadWorker(json.loads(Path(sys.argv[1]).read_text()))
worker.step()
''')
    process = subprocess.Popen([sys.executable, str(runner), str(config_path), str(environment_root)])
    executor = SystemdExecutor(config['worker'])
    attempt, worker, replacement_caller = None, None, None
    try:
        deadline = time.monotonic()+20
        journal_path = Path(config['state_dir'])/'active.json'
        while True:
            if journal_path.exists():
                journal = json.loads(journal_path.read_text())
                if journal['phase'] == 'running':
                    attempt = journal['assignment']['attempt_id']
                    group = executor.inspect(attempt).get('ControlGroup')
                    if group:
                        break
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        assignment = journal['assignment']
        handle = assignment['environment']['handle']
        assert handle == first['environment_handle']
        process.kill()
        process.wait(timeout=5)
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        deadline = time.monotonic()+10
        while cgroup.exists() and 'populated 1' in (cgroup/'cgroup.events').read_text():
            assert time.monotonic() < deadline
            time.sleep(.1)

        store, authority = store._test_restart_authority()
        config['authority'] = authority
        caller.close()
        replacement_caller = WorkloadClient(authority, 'a'*32)
        caller = replacement_caller
        worker = WorkloadWorker(config)
        worker.reconcile()
        assert caller.get(first['id'])['state'] == 'queued'
        marker_path = environment_root/handle/'environment.json'
        marker = json.loads(marker_path.read_text())
        assert marker['state'] == 'rebuild_required'
        assert marker['compatibility'] == '0'*64
        assert not any(replica['handle'] == handle for replica in worker.task_environments.report()[1])
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state!='ended'").fetchone()[0] == 0
        caller.request('jobs/'+first['id']+'/cancel', {})

        second = caller.submit(dict(version=3, key='crash-recovery-second', handler='native.v1',
            input_digest=digest, need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
            environment={'key':'crash-recovery-task','reuse':'prefer'},
            payload={'cache':True,'inspect_cache':True}))
        assert second['environment_handle'] == handle
        assert worker.step()
        result = caller.get(second['id'])
        assert result['state'] == 'succeeded'
        receipt = result['result']['environment_receipt']
        assert receipt['reuse_outcome'] == 'rebuilt'
        assert receipt['reason_code'] == 'authority_replica_unconfirmed'
        artifact = next(item for item in result['result']['result']['artifacts'] if item['name'] == 'artifact')
        returned = InputTransfer(caller).get(artifact['digest'], tmp_path/'recovered-artifact')
        assert 'cache=fresh' in returned.read_text()
        assert 'partial-cache' not in returned.read_text()
        assert caller.get_environment(handle)['state'] == 'parked'
        stale_worker = WorkloadClient(config['authority'], config['token'])
        timings = {phase: {'seconds': None, 'reason': 'measurement_unavailable'} for phase in
                   ('queue', 'transfer', 'source_materialization', 'dependencies', 'compile', 'test',
                    'execution', 'cleanup')}
        stale_receipt = dict(version=1, handle=handle, generation=assignment['environment']['generation'],
            profile=assignment['environment']['profile'],
            compatibility=assignment['environment']['compatibility'], source_digest=digest,
            reuse_outcome='rebuilt', reason_code='authority_replica_unconfirmed', state='parked',
            bytes_used=0, phase_timings=timings, cache_components=[])
        with pytest.raises(WorkloadError) as refused:
            stale_worker.request('worker/complete', dict(boot=assignment['boot'],
                attempt_id=assignment['attempt_id'], fence=assignment['fence'], input_digest=digest,
                outcome='succeeded', result={'exit_code': 0, 'artifacts': []},
                environment_receipt=stale_receipt))
        assert refused.value.status == 409
        assert worker.journal.read() is None
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state!='ended'").fetchone()[0] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if attempt:
            executor.stop(attempt)
        if worker:
            worker.close()
        if replacement_caller:
            replacement_caller.close()


@pytest.mark.parametrize('backend', ['rootless-docker', 'rootless-docker-native'])
@pytest.mark.parametrize('exit_code, expected', [(0, 'succeeded'), (7, 'failed'), (75, 'queued')])
def test_rootless_worker_delivers_pinned_artifact(fleet, tmp_path, backend, exit_code, expected):
    import shutil
    if not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd')):
        pytest.skip('requires installed rootless Docker prerequisites')
    store, config, caller, digest = fleet
    config['handlers']['native.v1'].update(backend=backend, infrastructure_outputs=['artifact'])
    if backend == 'rootless-docker-native':
        address = os.environ.get('HARMONY_TEST_NATIVE_HOST_ADDRESS')
        if address is None:
            pytest.skip('requires an explicit operator-declared native test host address')
        config['docker_native_host_address'] = address
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
        if exit_code == 75:
            assert result['result']['outcome'] == 'infrastructure'
            caller.request('jobs/'+job['id']+'/cancel', {})
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


def test_infrastructure_verdict_ships_undeclared_run_logs(fleet, tmp_path):
    """The run's own logs are the evidence of why execution died. An
    infrastructure verdict ships every *.log even when the handler declared
    none, and nothing that is not a log."""
    _, config, caller, digest = fleet
    script = Path(config['handlers']['native.v1']['argv'][1])
    script.write_text('''import os
from pathlib import Path
Path(os.environ['HARMONY_OUTPUT'],'run.log').write_text('the tail says why')
Path(os.environ['HARMONY_OUTPUT'],'notes.txt').write_text('not evidence')
raise SystemExit(75)
''')
    config['handlers']['native.v1'].update(infrastructure_exit_codes=[75])
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['result']['outcome'] == 'infrastructure'
        refs = result['result']['result']['artifacts']
        names = {item['name'] for item in refs}
        assert 'run.log' in names and 'notes.txt' not in names
        log = next(item for item in refs if item['name'] == 'run.log')
        assert InputTransfer(caller).get(log['digest'], tmp_path/'run.log').read_text() == 'the tail says why'
    finally:
        worker.close()


def test_infrastructure_failure_records_the_cause_a_caller_can_reach(fleet, tmp_path, monkeypatch):
    """A starved link used to end attempts with `detail: download retry budget
    exhausted` and no underlying error anywhere a caller could reach. Every
    infrastructure verdict carries the failing cause."""
    _, config, caller, digest = fleet
    job = submit(caller, digest)
    worker = WorkloadWorker(config)
    def broken(*_args, **_kwargs):
        raise EOFError('truncated block on a starved link')
    monkeypatch.setattr(worker.transfer, 'get', broken)
    try:
        assert worker.step()
        result = caller.get(job['id'])
        assert result['state'] == 'queued' and result['reason'] == 'infrastructure retry'
        assert result['result']['outcome'] == 'infrastructure'
        detail = result['result']['result']
        assert detail['error'] == 'EOFError'
        assert detail['detail'] == 'truncated block on a starved link'
        assert worker.journal.read() is None
        caller.request('jobs/'+job['id']+'/cancel', {})
    finally:
        worker.close()


def host_reading(path, **fields):
    path.write_text(json.dumps(fields))
    return dict(path=str(path), max_age_seconds=30)


@pytest.mark.parametrize('reading, expected', [
    (dict(ts=0, available_memory_bytes=32*1024**2), 32*1024**2),      # host is tighter than guest
    (dict(ts=0, available_memory_bytes=1024**4), 128*1024**2),        # guest stays the ceiling
    (dict(ts=0, available_memory_bytes=0), 0),                        # host thrashing
    (dict(ts=-3600, available_memory_bytes=1024**4), 0),             # stale is NOT "no pressure"
    (dict(ts=0, available_memory_bytes='lots'), 0),
    (dict(ts=0, available_memory_bytes=True), 0),
    (dict(available_memory_bytes=1024**4), 0),
])
def test_host_pressure_caps_reported_memory_and_fails_closed(fleet, tmp_path, reading, expected):
    _, config, _, _ = fleet
    # ts is an offset from now: parametrize values are built at collection time,
    # long before a slow suite reaches this test.
    reading = {k: (time.time()+v if k == 'ts' else v) for k, v in reading.items()}
    config['host_pressure'] = host_reading(tmp_path/'host-pressure.json', **reading)
    worker = WorkloadWorker(config)
    try:
        assert worker.report()['available']['memory_bytes'] == expected
    finally:
        worker.close()


def test_missing_or_unparseable_host_pressure_file_reports_zero_and_recovers(fleet, tmp_path):
    _, config, _, _ = fleet
    path = tmp_path/'host-pressure.json'
    config['host_pressure'] = dict(path=str(path))
    worker = WorkloadWorker(config)
    try:
        assert worker.report()['available']['memory_bytes'] == 0        # no file
        path.write_text('{not json')
        assert worker.report()['available']['memory_bytes'] == 0        # torn write
        path.write_text(json.dumps(dict(ts=time.time(), available_memory_bytes=64*1024**2)))
        assert worker.report()['available']['memory_bytes'] == 64*1024**2
    finally:
        worker.close()


def test_without_host_pressure_config_memory_is_unchanged(fleet):
    _, config, _, _ = fleet
    worker = WorkloadWorker(config)
    try:
        assert 'host_pressure' not in config
        assert worker.report()['available']['memory_bytes'] > 0
    finally:
        worker.close()


def test_client_cancels_a_queued_job_and_reports_each_outcome(fleet):
    from livestack_node.workloads.cli import cancel_jobs
    _, _, caller, digest = fleet
    job = submit(caller, digest)
    assert [item['id'] for item in caller.list_jobs()] == [job['id']]
    outcomes = cancel_jobs(caller, [job['id'], job['id'], 'j'*32, 'not a job id!'])
    assert outcomes[0] == dict(job=job['id'], state='cancelled', reason='cancelled by owner')
    assert outcomes[1]['state'] == 'cancelled'             # idempotent on a terminal job
    assert outcomes[2]['error'] == 'job not found'         # a refusal is named, not swallowed
    assert 'error' in outcomes[3]                           # malformed ids do not stop the batch
    assert caller.get(job['id'])['state'] == 'cancelled'


def test_another_principal_cannot_cancel_a_job(fleet):
    store, config, caller, digest = fleet
    job = submit(caller, digest)
    stranger = WorkloadClient(config['authority'], 'w'*32)   # the worker principal, not the owner
    with pytest.raises(WorkloadError):
        stranger.cancel(job['id'])
    assert caller.get(job['id'])['state'] == 'queued'


class Outage:
    """A real TCP hop between worker and authority that can refuse connections
    (down/up on the SAME port) or reset the first N result uploads. Going down
    also cuts the flows already open: the worker keeps its control connections
    alive, and an outage that spared them would not be one."""

    def __init__(self, upstream_port):
        self.upstream, self.reset_puts, self.listener = upstream_port, 0, None
        self.flows = set()
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.up()

    def up(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', self.port))
        listener.listen(32)
        self.listener = listener
        Thread(target=self._accept, args=(listener,), daemon=True).start()

    def down(self):
        # close() alone does not wake a thread blocked in accept(), which
        # would admit one more connection; a kept-alive one then outlives
        # the outage.
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        for sock in list(self.flows):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _accept(self, listener):
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        self.flows.add(client)
        upstream = None
        try:
            first = client.recv(65536)
            if first.startswith(b'PUT ') and self.reset_puts > 0:
                self.reset_puts -= 1
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
                client.close()
                return
            upstream = socket.create_connection(('127.0.0.1', self.upstream), timeout=30)
            self.flows.add(upstream)
            upstream.sendall(first)
            def pump(a, b):
                try:
                    while (chunk := a.recv(65536)):
                        b.sendall(chunk)
                    b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            Thread(target=pump, args=(client, upstream), daemon=True).start()
            pump(upstream, client)
            client.close()
            upstream.close()
        except OSError:
            pass
        finally:
            self.flows.discard(client)
            self.flows.discard(upstream)


@pytest.fixture
def outage(fleet):
    store, config, caller, digest = fleet
    store.limits = Limits(lease_seconds=4)
    hop = Outage(int(config['authority'].rsplit(':', 1)[1]))
    config.update(authority=f'http://127.0.0.1:{hop.port}', status_report_seconds=.3,
                  handoff_retry_seconds=20)
    yield hop, store, config, caller, digest
    hop.down()


def run_step(worker):
    errors = []
    def go():
        try:
            worker.step()
        except Exception as error:
            errors.append(error)
    thread = Thread(target=go)
    thread.start()
    return thread, errors


def wait_running(worker):
    deadline = time.monotonic()+10
    while not ((journal := worker.journal.read()) and journal['phase'] == 'running'):
        assert time.monotonic() < deadline
        time.sleep(.05)
    time.sleep(.5)
    return journal['assignment']


def test_short_authority_outage_does_not_stop_a_healthy_attempt(outage, caplog):
    hop, store, config, caller, digest = outage
    # Keep enough lease headroom for scheduler pauses during the whole worker
    # module; the simulated authority outage remains much shorter than this.
    store.limits = Limits(lease_seconds=60)
    caplog.set_level(logging.INFO)
    job = submit(caller, digest, sleep=6)
    worker = WorkloadWorker(config)
    thread, errors = run_step(worker)
    try:
        assignment = wait_running(worker)
        hop.down()
        time.sleep(1.5)  # shorter than the lease; long enough to hit several heartbeats/reports
        hop.up()
        thread.join(timeout=30)
        assert not thread.is_alive() and errors == []
        assert 'stopped:' not in caplog.text and 'lease lost' not in caplog.text
        assert 'authority unreachable, retrying (lease has' in caplog.text
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded'
        assert [a['fence'] for a in result['attempts']] == [assignment['fence']]
    finally:
        thread.join(timeout=30)
        worker.close()


def test_authority_outage_longer_than_the_lease_stops_and_fences_the_attempt(outage, caplog):
    hop, store, config, caller, digest = outage
    caplog.set_level(logging.INFO)
    job = submit(caller, digest, sleep=60)
    worker = WorkloadWorker(config)
    thread, errors = run_step(worker)
    started = time.monotonic()
    try:
        wait_running(worker)
        hop.down()
        deadline = time.monotonic()+15
        while 'lease lost' not in caplog.text:
            assert time.monotonic() < deadline, caplog.text
            time.sleep(.1)
        assert 'LeaseExpired' in caplog.text
        hop.up()
        thread.join(timeout=30)
        assert not thread.is_alive() and time.monotonic()-started < 40
        worker.reconcile()
        result = caller.get(job['id'])
        assert result['state'] == 'queued'  # fenced, not running on
        assert all(a['state'] != 'running' for a in result['attempts'])
        assert worker.journal.read() is None
    finally:
        thread.join(timeout=30)
        caller.request('jobs/'+job['id']+'/cancel', {})
        worker.close()


def test_transient_failure_during_result_upload_is_retried(outage, caplog):
    hop, store, config, caller, digest = outage
    caplog.set_level(logging.INFO)
    job = submit(caller, digest)
    hop.reset_puts = 2
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        assert hop.reset_puts == 0
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded'
        assert 'result upload: authority unreachable' in caplog.text
        assert worker.journal.read() is None
    finally:
        worker.close()


def _stale_attempt(worker, name='5'*32, read_only=True):
    """A leftover workspace as an e2e handler leaves it: read-only file inside a
    0555 directory, referenced by an abandoned journal entry."""
    root = Path(worker.workspace)/name
    (root/'source'/'sub').mkdir(parents=True)
    (root/'source'/'sub'/'query.test.mjs').write_text('x')
    if read_only:
        (root/'source'/'sub'/'query.test.mjs').chmod(0o444)
        (root/'source'/'sub').chmod(0o555)
        (root/'source').chmod(0o555)
    worker.journal.write(dict(assignment=dict(attempt_id=name), phase='abandoned'))
    return root


def test_reconcile_removes_read_only_stale_workspace(fleet):
    store, config, caller, digest = fleet
    worker = WorkloadWorker(config)
    try:
        root = _stale_attempt(worker)
        worker.reconcile()
        assert not root.exists() and worker.stuck_workspaces == {}
    finally:
        worker.close()


def test_unremovable_workspace_never_blocks_claiming_and_logs_once(fleet, monkeypatch, caplog):
    store, config, caller, digest = fleet
    worker = WorkloadWorker(config)
    try:
        root = _stale_attempt(worker)
        real = shutil.rmtree
        def refuse(path, *args, **kwargs):
            if '5'*32 in str(path):
                raise PermissionError(13, 'Permission denied', str(path))
            return real(path, *args, **kwargs)
        monkeypatch.setattr('livestack_node.workloads.worker.shutil.rmtree', refuse)
        job = submit(caller, digest)
        with caplog.at_level(logging.WARNING):
            assert worker.step()
            assert not worker.step()
        assert caller.get(job['id'])['state'] == 'succeeded'
        assert root.exists() and list(worker.stuck_workspaces) == [str(root)]
        lines = [r.getMessage() for r in caplog.records if 'workspace cleanup failed' in r.getMessage()]
        assert len(lines) == 1 and str(root) in lines[0] and 'PermissionError' in lines[0]
        monkeypatch.undo()
        assert not worker.step()
        assert not root.exists() and worker.stuck_workspaces == {}
    finally:
        worker.close()


def test_service_loop_logs_a_traceback_once_for_a_repeating_exception(caplog):
    from livestack_node.workloads.worker_service import serve

    class Broken:
        turns = 0
        def step(self):
            self.turns += 1
            raise PermissionError(13, 'Permission denied', 'query.test.mjs')

    worker = Broken()
    def sleep(seconds):
        if worker.turns >= 4:
            raise KeyboardInterrupt
    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
        serve(worker, sleep)
    messages = [r.getMessage() for r in caplog.records]
    assert sum('worker waiting after PermissionError' in m for m in messages) == 4
    traces = [m for m in messages if 'Traceback' in m]
    assert len(traces) == 1 and 'in step' in traces[0] and 'query.test.mjs' in traces[0]


def test_refused_runtime_cleanup_never_blocks_claiming_and_logs_once(fleet, tmp_path, monkeypatch, caplog):
    store, config, caller, digest = fleet
    monkeypatch.setenv('LIVESTACK_WORKLOAD_RUNTIME_BASE', str(tmp_path))
    from livestack_node.workloads.docker_runtime import runtime_path
    worker = WorkloadWorker(config)
    try:
        attempt = '6'*32
        stuck = runtime_path(worker.executor.unit(attempt))
        stuck.mkdir(mode=0o700)
        (stuck/'owner.json').write_text(json.dumps({'unit': 'someone-else'}))
        worker._stop(attempt)
        job = submit(caller, digest)
        with caplog.at_level(logging.WARNING):
            assert worker.step()
            assert not worker.step()
        assert caller.get(job['id'])['state'] == 'succeeded'
        assert stuck.exists() and worker.stuck_runtimes == {attempt}
        lines = [r.getMessage() for r in caplog.records if 'docker runtime cleanup failed' in r.getMessage()]
        assert len(lines) <= 1
        (stuck/'owner.json').unlink()
        assert not worker.step()
        assert not stuck.exists() and worker.stuck_runtimes == set()
    finally:
        worker.close()


def test_report_carries_the_measured_host_and_every_running_attempt(fleet):
    """openspec/changes/host-memory-ledger: no `capacity` means the measured
    machine, and the report names each running attempt's real cgroup memory so a
    SIBLING identity's report (here a second HostView) charges it too."""
    from livestack_node.hostview import HostView, meminfo
    store, config, caller, digest = fleet
    del config['capacity']
    config['status_report_seconds'] = .2
    job = submit(caller, digest, sleep=3)
    worker = WorkloadWorker(config)
    stepped = Thread(target=worker.step)
    try:
        stepped.start()
        sibling, seen = HostView(), None
        deadline = time.monotonic()+20
        while time.monotonic() < deadline and not seen:
            attempts = caller.get(job['id'])['attempts']
            if attempts:
                seen = sibling.sample()['attempts'].get(attempts[0]['id'])
            time.sleep(.05)
        assert seen and seen > 0, 'the running attempt cgroup must be visible host-wide'
        with store.transaction() as db:
            report = json.loads(db.execute('SELECT report FROM workers').fetchone()['report'])
        assert report['capacity']['memory_bytes'] == meminfo()['total']
        assert report['host']['memory_total_bytes'] == meminfo()['total']
        assert set(report['host']['psi']) == {'memory', 'io', 'cpu'}
        stepped.join(timeout=30)
        resources = caller.get(job['id'])['result']['result']['resources']
        assert resources['memory_peak_bytes'] > 0
        # The worker's own sample of what the kernel cannot drop as cache.
        assert 0 < resources['memory_nonreclaimable_peak_bytes'] <= resources['memory_peak_bytes']
    finally:
        stepped.join(timeout=30)
        worker.close()


def test_transfer_max_bytes_sets_the_worker_wide_object_ceiling(fleet, tmp_path, caplog):
    # Real worker + authority: the setting reaches InputTransfer, which refuses
    # an upload over it before any byte is sent (the success path is the 8 GiB test above).
    _, config, _caller, _digest = fleet
    config['transfer_max_bytes'] = 64
    with caplog.at_level(logging.INFO):
        worker = WorkloadWorker(config)
    try:
        assert 'object transfer ceiling is 64 bytes' in caplog.text
        assert worker.transfer.max_bytes == 64
        large = tmp_path/'large'
        large.write_bytes(b'l'*65)
        with pytest.raises(WorkloadError, match='upload byte limit exceeded'):
            worker.transfer.put(large)
        # A handler without output_max_bytes follows the worker-wide ceiling.
        assert worker.output_transfers['native.v1'] is worker.transfer
    finally:
        worker.close()


def test_transfer_max_bytes_defaults_to_two_gib(fleet):
    _, config, _caller, _digest = fleet
    worker = WorkloadWorker(config)
    try:
        assert worker.transfer.max_bytes == 2 * 1024**3
    finally:
        worker.close()


@pytest.mark.parametrize('invalid_limit', [True, 0, -1, 8 * 1024**3 + 1, 1.5, '8589934592'])
def test_worker_rejects_invalid_transfer_max_bytes(fleet, invalid_limit):
    _, config, _caller, _digest = fleet
    config['transfer_max_bytes'] = invalid_limit
    with pytest.raises(WorkloadError, match='transfer_max_bytes'):
        WorkloadWorker(config)


def test_transfer_max_bytes_accepts_eight_gib_without_touching_files(fleet):
    _, config, _caller, _digest = fleet
    config['transfer_max_bytes'] = 8 * 1024**3
    worker = WorkloadWorker(config)
    try:
        assert worker.transfer.max_bytes == 8 * 1024**3
    finally:
        worker.close()


def psi_file(path, full_avg60):
    path.write_text('some avg10=40.00 avg60=%.2f avg300=30.00 total=1\nfull avg10=0.00 avg60=%.2f avg300=0.00 total=1\n'
                    % (full_avg60+30, full_avg60))
    return str(path)


def test_default_cpu_admission_still_collapses_on_a_busy_host(fleet, monkeypatch):
    _, config, _, _ = fleet
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (99, 99, 99))
    worker = WorkloadWorker(config)
    try:
        assert worker.report()['available']['cpu'] == 0
    finally:
        worker.close()


@pytest.mark.parametrize('full, spec, expected', [
    (0.0, {}, 'capacity'),                                    # busy but not stalled: admissible
    (0.0, dict(reserve_cpu=0.25), 'capacity-0.25'),
    (4.9, {}, 'capacity'),
    (5.1, {}, 0),                                             # genuinely stalled
    (3.0, dict(stall_full_avg60_percent=2), 0),
])
def test_psi_cpu_admission_offers_capacity_unless_stalled(fleet, monkeypatch, tmp_path, full, spec, expected):
    _, config, _, _ = fleet
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (99, 99, 99))
    config['cpu_admission'] = dict(policy='psi', psi_path=psi_file(tmp_path/'cpu', full), **spec)
    worker = WorkloadWorker(config)
    try:
        report = worker.report()
        cpu = report['capacity']['cpu']
        want = {'capacity': cpu, 'capacity-0.25': cpu-0.25}.get(expected, expected)
        assert report['available']['cpu'] == want
    finally:
        worker.close()


def test_psi_cpu_admission_fails_closed_and_recovers(fleet, tmp_path):
    _, config, _, _ = fleet
    path = tmp_path/'cpu'
    config['cpu_admission'] = dict(policy='psi', psi_path=str(path))
    worker = WorkloadWorker(config)
    try:
        assert worker.report()['available']['cpu'] == 0           # missing file
        path.write_text('some avg60=1.00\n')                       # no `full` line
        assert worker.report()['available']['cpu'] == 0
        psi_file(path, 0.0)
        assert worker.report()['available']['cpu'] == worker.report()['capacity']['cpu'] > 0
    finally:
        worker.close()


@pytest.mark.parametrize('bad', [dict(policy='psi', stall_full_avg60_percent=101), dict(policy='bogus'),
                                 dict(polcy='psi'), dict(reserve_cpu=-1), dict(reserve_cpu='2')])
def test_invalid_cpu_admission_is_refused_at_construction(fleet, bad):
    _, config, _, _ = fleet
    config['cpu_admission'] = bad
    with pytest.raises(WorkloadError, match='invalid cpu_admission'):
        WorkloadWorker(config)


def test_worker_startup_path_needs_no_pydantic():
    """A stock interpreter without pydantic must still import the worker and judge
    cpu_admission (hand-managed hosts; a missing pydantic crash-looped zz-joe's first roll).
    Real subprocess with a meta-path finder that makes `import pydantic` fail."""
    script = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == 'pydantic' or name.startswith('pydantic.') or name == 'pydantic_core':
            raise ModuleNotFoundError("No module named 'pydantic'")
sys.meta_path.insert(0, Block())
from livestack_node.workloads import worker
from livestack_node.workloads.cpu_admission import CpuAdmission
assert 'pydantic' not in sys.modules
assert CpuAdmission.validate({}).policy == 'loadavg'
ok = CpuAdmission.validate(dict(policy='psi', stall_full_avg60_percent=2.5, reserve_cpu=1, psi_path='/x'))
assert (ok.policy, ok.stall_full_avg60_percent, ok.reserve_cpu, ok.psi_path) == ('psi', 2.5, 1, '/x')
bad = {
  "policy: Input should be 'loadavg', 'psi', 'psi_some' or 'runqueue'": dict(policy='bogus'),
  'polcy: Extra inputs are not permitted': dict(polcy='psi'),
  'stall_full_avg60_percent: Input should be less than or equal to 100': dict(stall_full_avg60_percent=101),
  'reserve_cpu: Input should be greater than or equal to 0': dict(reserve_cpu=-1),
  'reserve_cpu: Input should be a valid number': dict(reserve_cpu='2'),
  'psi_path: Input should be a valid string': dict(psi_path=3),
}
for message, block in bad.items():
    try:
        CpuAdmission.validate(block)
    except ValueError as exc:
        assert str(exc) == message, (str(exc), message)
    else:
        raise AssertionError(block)
'''
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _resources(caller, job):
    return caller.get(job['id'])['result']['result']['resources']


def test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from(fleet):
    # Positive controls (openspec measured-resource-declarations section 7): each
    # quantity is driven by a known amount and an idle run shows the opposite.
    _, config, caller, digest = fleet
    worker = WorkloadWorker(config)
    try:
        busy = submit(caller, digest, _need={'cpu': 1}, busy=1.0, write_mb=24, allocate_mb=32)
        assert worker.step()
        idle = submit(caller, digest, 'two', sleep=1.0)
        assert worker.step()
        loaded, quiet = _resources(caller, busy), _resources(caller, idle)
        assert loaded['source'] == quiet['source'] == 'receipt'
        assert 0.7e6 <= loaded['cpu_usage_usec'] <= 2.5e6 and quiet['cpu_usage_usec'] < 0.6e6
        assert 32*1024**2 <= loaded['memory_peak_bytes'] <= 32*1024**2+64*1024**2
        assert loaded['memory_peak_bytes'] > quiet['memory_peak_bytes'] + 20*1024**2
        # filesystem delta: an upper bound (other writers on the filesystem count too)
        assert loaded['disk_delta_bytes'] >= 0.9*24*1024**2
        # no idle upper bound: the delta is filesystem-wide, so concurrent writers inflate it
        assert isinstance(quiet['disk_delta_bytes'], int)
    finally:
        worker.close()


def test_oom_kill_without_a_receipt_fails_as_a_resource_limit_and_is_not_retried(fleet):
    # The unit is killed as a whole (OOMPolicy=kill) so the wrapper never writes a
    # receipt. On origin/main this ends as "execution stopped without a result",
    # an infrastructure outcome that is retried.
    _, config, caller, digest = fleet
    job = submit(caller, digest, allocate_mb=400, sleep=5)
    worker = WorkloadWorker(config)
    try:
        assert worker.step()
        done = caller.get(job['id'])
        assert done['state'] == 'failed', done
        assert done['reason'].startswith('resource limit: memory'), done['reason']
        assert done['result']['cause']['cause'] == 'resource_limit'
        assert done['result']['cause']['kind'] == 'memory' and done['result']['cause']['retryable'] is False
        resources = done['result']['result']['resources']
        assert resources['oom_kill'] >= 1 and resources['source'] in ('unit', 'sampled')
        assert len(done['attempts']) == 1 and not worker.step()
        # The typed cause (openspec typed-outcome-causes-and-blockers) from the same real kernel kill.
        assert done['cause']['kind'] == 'oom_killed' and done['cause']['retry'] == 'after_change', done['cause']
        assert done['cause']['evidence']['source'] in ('unit', 'sampled')
    finally:
        worker.close()
