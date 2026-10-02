"""Real LocalSystem verifier service, real Job Objects and a real MSVC toolchain on Windows.

openspec/changes/windows-host-worker, design "Verifier". Requires a Windows host,
an administrator (to create a disposable service and a protected ProgramData
directory) and rustc with the x86_64-pc-windows-msvc target for the positive
control. Never touches an enrolled worker or verifier: the service, pipe, job
names and directories are disposable.
"""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows verifier controls')

from test_workload_compilation_policy import claim  # noqa: E402
from livestack_node.workloads.client import WorkloadClient  # noqa: E402
from livestack_node.workloads.compilation_policy import CompilationPolicy  # noqa: E402
from livestack_node.workloads.http import Principal, WorkloadServer  # noqa: E402
from livestack_node.workloads.launch_contract import verify_launch  # noqa: E402
from livestack_node.workloads.model import Limits, WorkloadError  # noqa: E402
from livestack_node.workloads.store import WorkloadStore  # noqa: E402
from livestack_node.workloads.supervision import WorkerJournal  # noqa: E402

if sys.platform == 'win32':
    from livestack_node.workloads import windows_proc
    from livestack_node.workloads.windows_pipe import PIPE_PREFIX, PipeListener
    from livestack_node.workloads.windows_supervision import JobObjectExecutor

NODE_PY = str(Path(__file__).resolve().parents[1])
# MSVC discovery (vswhere / the VS setup API) needs the ProgramFiles variables.
ENV = {k: v for k, v in os.environ.items()
       if k.upper() in ('SYSTEMROOT', 'PATH', 'WINDIR', 'COMSPEC', 'USERPROFILE', 'RUSTUP_HOME', 'CARGO_HOME',
                        'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'SYSTEMDRIVE')}


def until(predicate, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


def _sc(*args):
    return subprocess.run(['sc.exe', *args], capture_output=True, text=True, timeout=60)


def _state(name):
    for line in _sc('query', name).stdout.splitlines():
        if 'STATE' in line:
            return line.split()[-1]
    return None


def protected_directory():
    """A ProgramData directory only SYSTEM and Administrators can touch."""
    root = Path(os.environ['ProgramData'])/('livestack-it-'+uuid.uuid4().hex[:12])
    root.mkdir()
    subprocess.run(['icacls', str(root), '/inheritance:r', '/grant:r', 'SYSTEM:(OI)(CI)F',
                    'Administrators:(OI)(CI)F'], check=True, capture_output=True)
    return root


@pytest.fixture
def authority(tmp_path, monkeypatch):
    """Disposable real HTTP/SQLite authority whose handler is classified
    [windows, rust] and whose policy grants both to this host only."""
    # The policy reader's ownership test is POSIX (the authority runs on Linux);
    # on this Windows test host the file is ours and read-only.
    monkeypatch.setattr(os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(os, 'O_NOFOLLOW', 0, raising=False)
    monkeypatch.setattr(os, 'O_NONBLOCK', 0, raising=False)
    policy_path = tmp_path/'policy.json'
    policy_path.write_text(json.dumps(dict(version=1, revision='windows-1', expires=time.time()+3600,
        hosts={'physical-builder': ['windows', 'rust']}, enrollments={'builder': 'physical-builder'})))
    policy_path.chmod(0o444)
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'windows.v1'}, limits=Limits(),
        compilation_policy=CompilationPolicy(policy_path, {'windows.v1': ['windows', 'rust']}))
    principals = [Principal('caller', 'c'*32, 'caller', ('windows.v1',)),
                  Principal('builder', '1'*32, 'worker', worker='builder', host='builder')]
    server = WorkloadServer(('127.0.0.1', 0), store, principals)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    clients = {p.id: WorkloadClient(url, p.token) for p in principals}
    data = b'windows compilation source fixture'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('caller', digest, len(data), BytesIO(data))
    clients['caller'].request('jobs', dict(version=1, key='launch', handler='windows.v1',
        input_digest=digest, need={'cpu': 1, 'memory_bytes': 768*1024**2}))
    resources = {'cpu': 1, 'memory_bytes': 1024**3}
    clients['builder'].request('worker/report', dict(boot='boot', report=dict(
        capacity=resources, available=resources, labels={}, handlers=['windows.v1'], ready=True)))
    yield clients
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def install_verifier(root, config, name):
    path = root/'config.json'
    path.write_text(json.dumps(config))
    python = getattr(sys, '_base_executable', sys.executable)
    command = (f'"{python}" -m livestack_node.workloads.launch_verifier --config "{path}" '
               f'--service-name {name}')
    assert _sc('create', name, 'binPath=', command, 'start=', 'demand').returncode == 0
    subprocess.run(['reg', 'add', rf'HKLM\SYSTEM\CurrentControlSet\Services\{name}', '/v', 'Environment',
                    '/t', 'REG_MULTI_SZ', '/d', f'PYTHONPATH={NODE_PY}\\0PYTHONDONTWRITEBYTECODE=1', '/f'],
                   check=True, capture_output=True)
    _sc('start', name)


def remove_service(name):
    _sc('stop', name)
    until(lambda: _state(name) in (None, 'STOPPED'), 30)
    _sc('delete', name)


@pytest.fixture
def verifier(authority, tmp_path):
    if subprocess.run(['net', 'session'], capture_output=True).returncode:
        pytest.skip('installing a LocalSystem verifier needs an administrator')
    clients = authority
    assignment = claim(clients, 'builder')
    journal = WorkerJournal(tmp_path/'journal')
    journal.write(dict(assignment=assignment, phase='running'))
    root = protected_directory()
    pipe = PIPE_PREFIX+'it-'+uuid.uuid4().hex[:12]
    config = dict(version=1, worker='builder', worker_sid=windows_proc.current_user_sid(),
                  host='physical-builder', machine_id=windows_proc.machine_guid(),
                  authority=clients['builder'].url.removesuffix('/v1/workloads/'),
                  token=clients['builder'].token, journal=str(journal.path), pipe=pipe)
    (root/'registry.json').write_text(json.dumps({'version': 1, 'slots': {'builder': pipe}}))
    name = 'LivestackVerifierTest'+uuid.uuid4().hex[:8]
    install_verifier(root, config, name)
    executor = JobObjectExecutor('builder')
    request = {key: assignment[key] for key in ('worker', 'boot', 'job_id', 'attempt_id', 'fence')}
    request.update(version=1, host=assignment['compilation']['host'],
                   policy_revision=assignment['compilation']['policy_revision'],
                   input_digest=assignment['spec']['input_digest'], **{'class': 'windows'})
    try:
        until(lambda: (root/'verifier.log').exists() and 'listening' in (root/'verifier.log').read_text(), 30)
        yield dict(assignment=assignment, executor=executor, request=request, registry=str(root/'registry.json'),
                   root=root, tmp=tmp_path, config=config)
    finally:
        executor.stop(assignment['attempt_id'])
        remove_service(name)
        shutil.rmtree(root, ignore_errors=True)
        journal.close()


IMPORTS = '''import json,os,subprocess
from pathlib import Path
from livestack_node.workloads.launch_contract import verify_launch
request=json.loads(Path(os.environ['TEST_REQUEST']).read_text())
receipt=verify_launch(request,registry_path=os.environ['TEST_REGISTRY'])
'''


def launch(v, script, *, memory=768*1024**2, cpu=1):
    root = v['tmp']
    path = root/'request.json'
    path.write_text(json.dumps(v['request']))
    program = root/'launch.py'
    program.write_text(script)
    output = root/'out'
    env = dict(ENV, PYTHONPATH=NODE_PY, TEST_REQUEST=str(path), TEST_REGISTRY=v['registry'], TEST_ROOT=str(root))
    v['executor'].start(v['assignment']['attempt_id'], [sys.executable, str(program)], root, output,
                        env=env, cpu=cpu, memory_bytes=memory, max_seconds=180, tasks=64)
    return output


def test_real_admitted_msvc_toolchain_launch_positive_control(verifier):
    root = verifier['tmp']
    (root/'tiny.rs').write_text('fn main() { std::process::exit(17) }\n')
    output = launch(verifier, IMPORTS+'''
root=Path(os.environ['TEST_ROOT'])
subprocess.run(['rustc','--target','x86_64-pc-windows-msvc',str(root/'tiny.rs'),'-o',str(root/'tiny.exe')],check=True)
assert subprocess.run([str(root/'tiny.exe')]).returncode==17
assert (root/'tiny.exe').read_bytes()[:2]==b'MZ'
(root/'compiled.json').write_text(json.dumps(receipt))
''')
    result = until(lambda: verifier['executor'].exit_result(output), 120)
    assert result['exit_code'] == 0, (output/'command.log').read_text()
    receipt = json.loads((root/'compiled.json').read_text())
    assert receipt['host'] == 'physical-builder' and 'windows' in receipt['classes']
    assert 'compilation_launch_admitted' in (verifier['root']/'verifier.log').read_text()


def test_copied_metadata_outside_job_refuses(verifier):
    verifier['executor'].start(verifier['assignment']['attempt_id'],
                               [sys.executable, '-c', 'import time; time.sleep(30)'], verifier['tmp'],
                               verifier['tmp']/'out', env=ENV, cpu=.5, memory_bytes=128*1024**2,
                               max_seconds=60, tasks=8)
    with pytest.raises(WorkloadError, match='compilation_peer_outside_attempt'):
        verify_launch(verifier['request'], registry_path=verifier['registry'])


@pytest.mark.parametrize('limits', [{'memory': 1024**3}, {'cpu': 2}])
def test_job_limits_cannot_exceed_authorized_execution_resources(verifier, limits):
    root = verifier['tmp']
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n",
                    **limits)
    result = until(lambda: verifier['executor'].exit_result(output), 30)
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_attempt_resource_limit_mismatch' in (output/'command.log').read_text()


def test_unreserved_class_refuses_in_real_job(verifier):
    verifier['request']['class'] = 'flutter'
    root = verifier['tmp']
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: verifier['executor'].exit_result(output), 30)
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_class_not_reserved' in (output/'command.log').read_text()


def test_registry_in_a_user_writable_directory_is_untrusted(verifier, tmp_path):
    loose = tmp_path/'registry.json'  # under the user's profile: the user owns it
    loose.write_text(Path(verifier['registry']).read_text())
    with pytest.raises(WorkloadError, match='compilation_registry_untrusted'):
        verify_launch(verifier['request'], registry_path=str(loose))


def test_pipe_served_by_a_non_system_process_is_refused(verifier):
    """A same-account process squatting a slot's pipe cannot answer for the verifier."""
    root = verifier['root']
    squat = PIPE_PREFIX+'squat-'+uuid.uuid4().hex[:8]
    listener = PipeListener(squat, windows_proc.current_user_sid())
    served = []

    def serve():
        stream, _ = listener.accept()
        served.append(stream)
    Thread(target=serve, daemon=True).start()
    registry = root/'squat-registry.json'
    registry.write_text(json.dumps({'version': 1, 'slots': {'builder': squat}}))
    time.sleep(.2)
    with pytest.raises(WorkloadError, match='compilation_verifier_peer_untrusted'):
        verify_launch(verifier['request'], registry_path=str(registry))


def test_copied_verifier_configuration_on_wrong_machine_cannot_start(verifier):
    root = protected_directory()
    name = 'LivestackVerifierTest'+uuid.uuid4().hex[:8]
    try:
        install_verifier(root, dict(verifier['config'], machine_id='0'*32,
                                    pipe=PIPE_PREFIX+'wrong-'+uuid.uuid4().hex[:8]), name)
        log = root/'verifier.log'
        until(lambda: log.exists() and 'service thread ended' in log.read_text(), 30)
        assert 'compilation_verifier_physical_machine_mismatch' in log.read_text()
    finally:
        remove_service(name)
        shutil.rmtree(root, ignore_errors=True)
