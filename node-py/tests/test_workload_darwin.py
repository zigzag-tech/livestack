"""Real launchd jobs, real root verifier and a real Apple toolchain on macOS.

openspec/changes/apple-host-compilation. Requires a macOS host with a GUI
session for the test user (launchd `gui/<uid>` domain), passwordless sudo and
Xcode command-line tools. Never touches an enrolled worker: every job label is
derived from a disposable worker id and every verifier is a disposable root
process with its own runtime directory, socket and registry.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS launchd/libproc controls')

from test_workload_compilation_policy import claim  # noqa: E402
import hashlib  # noqa: E402
from io import BytesIO  # noqa: E402
from threading import Thread  # noqa: E402
from livestack_node.workloads.client import WorkloadClient  # noqa: E402
from livestack_node.workloads.compilation_policy import CompilationPolicy  # noqa: E402
from livestack_node.workloads.http import Principal, WorkloadServer  # noqa: E402
from livestack_node.workloads.model import Limits  # noqa: E402
from livestack_node.workloads.store import WorkloadStore  # noqa: E402
from livestack_node.workloads.launch_contract import verify_launch  # noqa: E402
from livestack_node.workloads.model import WorkloadError  # noqa: E402
from livestack_node.workloads.supervision import WorkerJournal  # noqa: E402

if sys.platform == 'darwin':
    from livestack_node.workloads import darwin_proc
    from livestack_node.workloads.darwin_supervision import LaunchdExecutor, launchd_job

PYTHONPATH = str(Path(__file__).resolve().parents[1])


def until(predicate, seconds=10):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


@pytest.fixture
def executor():
    executor = LaunchdExecutor('darwin-test-'+uuid.uuid4().hex[:12])
    started = []
    original = executor.start

    def start(attempt, *args, **kwargs):
        started.append(attempt)
        return original(attempt, *args, **kwargs)
    executor.start = start
    yield executor
    for attempt in started:
        executor.stop(attempt)


def test_stop_kills_grandchild_and_escaped_group_member(executor, tmp_path):
    attempt = uuid.uuid4().hex
    # `( sleep & )` reparents its sleep to launchd: only the process group
    # still ties it to the attempt.
    executor.start(attempt, ['/bin/sh', '-c', '(/bin/sleep 300 &); /bin/sleep 300'], tmp_path, tmp_path/'out',
                   env={'PATH': '/usr/bin:/bin'}, cpu=1, memory_bytes=256*1024**2, max_seconds=60, tasks=16)
    job = launchd_job(executor.domain, executor.unit(attempt))
    assert job['state'] == 'running'
    root = darwin_proc.info(job['pid'])
    found = until(lambda: (lambda t: t if len(t) >= 4 else None)(darwin_proc.tree(root['pid'], root['start'])))
    assert any(r['ppid'] == 1 for p, r in found.items() if p != root['pid'])
    executor.stop(attempt)
    assert executor.inspect(attempt)['LoadState'] == 'not-found'
    assert darwin_proc.survivors(found) == {}


def test_memory_breach_kills_tree_and_records_oom(executor, tmp_path):
    attempt = uuid.uuid4().hex
    output = tmp_path/'out'
    executor.start(attempt, ['/usr/bin/python3', '-c', 'x=bytearray(600*1024*1024); import time; time.sleep(60)'],
                   tmp_path, output, env={'PATH': '/usr/bin:/bin'}, cpu=1, memory_bytes=256*1024**2,
                   max_seconds=60, tasks=16)
    result = until(lambda: executor.exit_result(output), 30)
    assert result['exit_code'] != 0
    assert result['resources']['oom_kill'] == 1
    assert result['resources']['memory_peak_bytes'] > 256*1024**2


def test_task_cap_kills_fork_fanout(executor, tmp_path):
    attempt = uuid.uuid4().hex
    output = tmp_path/'out'
    executor.start(attempt, ['/bin/sh', '-c', 'for i in 1 2 3 4 5 6 7 8 9 10; do /bin/sleep 60 & done; wait'],
                   tmp_path, output, env={'PATH': '/usr/bin:/bin'}, cpu=1, memory_bytes=256*1024**2,
                   max_seconds=60, tasks=4)
    result = until(lambda: executor.exit_result(output), 30)
    assert result['resources']['pids_max_events'] == 1


def test_restarted_worker_finds_job_by_label(executor, tmp_path):
    attempt = uuid.uuid4().hex
    executor.start(attempt, ['/bin/sleep', '300'], tmp_path, tmp_path/'out', env={'PATH': '/usr/bin:/bin'},
                   cpu=1, memory_bytes=64*1024**2, max_seconds=60, tasks=4)
    restarted = LaunchdExecutor(executor.worker_id)
    assert restarted.inspect(attempt)['ActiveState'] == 'active'
    restarted.stop(attempt)
    assert executor.inspect(attempt)['LoadState'] == 'not-found'


@pytest.fixture
def authority(tmp_path):
    """Disposable real HTTP/SQLite authority whose handler is classified
    [apple, rust] and whose policy grants both to this host only."""
    policy_path = tmp_path/'policy.json'
    policy_path.write_text(json.dumps(dict(version=1, revision='apple-1', expires=time.time()+3600,
        hosts={'physical-builder': ['apple', 'rust']}, enrollments={'builder': 'physical-builder'})))
    policy_path.chmod(0o600)
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'apple.v1'}, limits=Limits(),
        compilation_policy=CompilationPolicy(policy_path, {'apple.v1': ['apple', 'rust']}))
    principals = [Principal('caller', 'c'*32, 'caller', ('apple.v1',)),
                  Principal('builder', '1'*32, 'worker', worker='builder', host='builder')]
    server = WorkloadServer(('127.0.0.1', 0), store, principals)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    clients = {p.id: WorkloadClient(url, p.token) for p in principals}
    data = b'apple compilation source fixture'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('caller', digest, len(data), BytesIO(data))
    clients['caller'].request('jobs', dict(version=1, key='launch', handler='apple.v1',
        input_digest=digest, need={'cpu': 1, 'memory_bytes': 768*1024**2}))
    resources = {'cpu': 1, 'memory_bytes': 1024**3}
    clients['builder'].request('worker/report', dict(boot='boot', report=dict(
        capacity=resources, available=resources, labels={}, handlers=['apple.v1'], ready=True)))
    yield clients
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture
def verifier(authority, tmp_path):
    clients = authority
    assignment = claim(clients, 'builder')
    journal = WorkerJournal(tmp_path/'journal')
    journal.write(dict(assignment=assignment, phase='running'))
    runtime = '/private/var/harmony-launch-it-'+uuid.uuid4().hex[:12]
    registry = runtime+'/registry.json'
    endpoint = runtime+'/verify.sock'
    config = dict(version=1, worker='builder', worker_uid=os.getuid(), host='physical-builder',
                  machine_id=darwin_proc.platform_uuid(),
                  authority=clients['builder'].url.removesuffix('/v1/workloads/'),
                  token=clients['builder'].token, journal=str(journal.path), socket=endpoint)
    setup = '''import json,os,sys
from pathlib import Path
data=json.load(sys.stdin);root=Path(data['root']);root.mkdir(mode=0o755)
(root/'config.json').write_text(json.dumps(data['config']));(root/'config.json').chmod(0o600)
(root/'registry.json').write_text(json.dumps(data['registry']));(root/'registry.json').chmod(0o644)
'''
    # Root-owned interpreter for the root process: never a user-writable one.
    subprocess.run(['sudo', '-n', '/usr/bin/python3', '-c', setup], check=True,
                   input=json.dumps(dict(root=runtime, config=config,
                       registry={'version': 1, 'slots': {'builder': endpoint}})), text=True)
    log = (tmp_path/'verifier.log').open('w')
    process = subprocess.Popen(['sudo', '-n', '/usr/bin/env', 'PYTHONPATH='+PYTHONPATH, '/usr/bin/python3',
        '-m', 'livestack_node.workloads.launch_verifier', '--config', runtime+'/config.json'],
        stdout=log, stderr=log)
    executor = LaunchdExecutor('builder')
    request = {key: assignment[key] for key in ('worker', 'boot', 'job_id', 'attempt_id', 'fence')}
    request.update(version=1, host=assignment['compilation']['host'],
                   policy_revision=assignment['compilation']['policy_revision'],
                   input_digest=assignment['spec']['input_digest'], **{'class': 'apple'})
    try:
        until(lambda: Path(endpoint).exists() or process.poll() is not None)
        assert process.poll() is None, (tmp_path/'verifier.log').read_text()
        yield assignment, executor, request, registry, runtime, tmp_path
    finally:
        executor.stop(assignment['attempt_id'])
        subprocess.run(['sudo', '-n', '/usr/bin/pkill', '-TERM', '-f',
                        'livestack_node.workloads.launch_verifier --config '+runtime+'/config.json'],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait(timeout=5)
        log.close()
        subprocess.run(['sudo', '-n', '/bin/rm', '-rf', '--', runtime], check=True)
        journal.close()


IMPORTS = '''import json,os,subprocess
from pathlib import Path
from livestack_node.workloads.launch_contract import verify_launch
request=json.loads(Path(os.environ['TEST_REQUEST']).read_text())
receipt=verify_launch(request,registry_path=os.environ['TEST_REGISTRY'])
'''


def launch(verifier, script, *, memory=512*1024**2, cpu=1):
    assignment, executor, request, registry, _, root = verifier
    path = root/'request.json'
    path.write_text(json.dumps(request))
    program = root/'launch.py'
    program.write_text(script)
    output = root/'out'
    env = dict(PATH='/usr/bin:/bin:/usr/sbin', HOME=os.environ['HOME'], PYTHONPATH=PYTHONPATH,
               TEST_REQUEST=str(path), TEST_REGISTRY=registry, TEST_ROOT=str(root))
    executor.start(assignment['attempt_id'], ['/usr/bin/python3', str(program)], root, output,
                   env=env, cpu=cpu, memory_bytes=memory, max_seconds=120, tasks=64)
    return output


def test_real_admitted_apple_toolchain_launch_positive_control(verifier):
    _, executor, _, _, _, root = verifier
    (root/'tiny.c').write_text('int main(void) { return 17; }\n')
    output = launch(verifier, IMPORTS+'''
root=Path(os.environ['TEST_ROOT'])
subprocess.run(['/usr/bin/xcrun','clang',str(root/'tiny.c'),'-o',str(root/'tiny')],check=True)
assert subprocess.run([str(root/'tiny')]).returncode==17
assert b'Mach-O' in subprocess.check_output(['/usr/bin/file',str(root/'tiny')])
(root/'compiled.json').write_text(json.dumps(receipt))
''')
    result = until(lambda: executor.exit_result(output), 60)
    assert result['exit_code'] == 0, (output/'command.log').read_text()
    receipt = json.loads((root/'compiled.json').read_text())
    assert receipt['host'] == 'physical-builder' and 'apple' in receipt['classes']


def test_copied_metadata_outside_job_refuses(verifier):
    assignment, executor, request, registry, _, root = verifier
    executor.start(assignment['attempt_id'], ['/bin/sleep', '30'], root, root/'out',
                   env={'PATH': '/usr/bin:/bin'}, cpu=.5, memory_bytes=64*1024**2, max_seconds=60, tasks=4)
    with pytest.raises(WorkloadError, match='compilation_peer_outside_attempt'):
        verify_launch(request, registry_path=registry)


@pytest.mark.parametrize('limits', [{'memory': 1024**3}, {'cpu': 2}])
def test_job_limits_cannot_exceed_authorized_execution_resources(verifier, limits):
    _, executor, _, _, _, root = verifier
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n", **limits)
    result = until(lambda: executor.exit_result(output), 30)
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_attempt_resource_limit_mismatch' in (output/'command.log').read_text()


def test_unreserved_class_refuses_in_real_job(verifier):
    _, executor, request, _, _, root = verifier
    request['class'] = 'flutter'
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output), 30)
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_class_not_reserved' in (output/'command.log').read_text()


def test_copied_verifier_configuration_on_wrong_machine_cannot_start(verifier):
    _, _, _, _, runtime, _ = verifier
    change = '''import json,sys
from pathlib import Path
root=Path(sys.argv[1]);config=json.loads((root/'config.json').read_text())
config['machine_id']='0'*32;config['socket']=str(root/'wrong.sock')
path=root/'wrong-config.json';path.write_text(json.dumps(config));path.chmod(0o600)
'''
    subprocess.run(['sudo', '-n', '/usr/bin/python3', '-c', change, runtime], check=True)
    result = subprocess.run(['sudo', '-n', '/usr/bin/env', 'PYTHONPATH='+PYTHONPATH, '/usr/bin/python3',
        '-m', 'livestack_node.workloads.launch_verifier', '--config', runtime+'/wrong-config.json'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'compilation_verifier_physical_machine_mismatch' in result.stderr
