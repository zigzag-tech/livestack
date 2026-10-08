"""Real root verifier, authenticated authority, systemd cgroup and compiler.

Requires the disposable Linux test host's sudo and user manager. This suite
never starts a service against production authority or production worker state.
"""
import json
import os
import re
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from test_workload_compilation_policy import authority, claim
from livestack_node.workloads.launch_contract import verify_launch
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.supervision import SystemdExecutor, WorkerJournal
from livestack_node.workloads.lease import LeaseKeeper
from livestack_node.workloads.docker_runtime import remove_data


def until(predicate, seconds=10):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


@pytest.fixture
def verifier(authority, tmp_path):
    clients, submit, register, *_ = authority
    register('builder')
    submit('launch')
    assignment = claim(clients, 'builder')
    journal = WorkerJournal(tmp_path/'journal')
    journal.write(dict(assignment=assignment, phase='running'))
    runtime = '/run/harmony-launch-it-'+uuid.uuid4().hex[:16]
    registry = runtime+'/registry.json'
    endpoint = runtime+'/verify.sock'
    config = dict(version=1, worker='builder', worker_uid=os.getuid(), host='physical-builder',
                  machine_id=Path('/etc/machine-id').read_text().strip(),
                  authority=clients['builder'].url.removesuffix('/v1/workloads/'),
                  token=clients['builder'].token, journal=str(journal.path), socket=endpoint)
    setup = '''import json,os,sys
from pathlib import Path
data=json.load(sys.stdin);root=Path(data['root']);root.mkdir(mode=0o755)
(root/'config.json').write_text(json.dumps(data['config']));(root/'config.json').chmod(0o600)
(root/'registry.json').write_text(json.dumps(data['registry']));(root/'registry.json').chmod(0o644)
'''
    subprocess.run(['sudo', '-n', sys.executable, '-B', '-c', setup], check=True,
                   input=json.dumps(dict(root=runtime, config=config,
                       registry={'version': 1, 'slots': {'builder': endpoint}})), text=True)
    log = (tmp_path/'verifier.log').open('w')
    process = subprocess.Popen(['sudo', '-n', '/usr/bin/env',
        'PYTHONPATH='+str(Path(__file__).resolve().parents[1]), sys.executable, '-B',
        '-m', 'livestack_node.workloads.launch_verifier', '--config', runtime+'/config.json'],
        stdout=log, stderr=log)
    executor = SystemdExecutor('builder')
    request = {key: assignment[key] for key in ('worker', 'boot', 'job_id', 'attempt_id', 'fence')}
    request.update(version=1, host=assignment['compilation']['host'],
                   policy_revision=assignment['compilation']['policy_revision'],
                   input_digest=assignment['spec']['input_digest'], **{'class': 'rust'})
    try:
        until(lambda: Path(endpoint).exists() or process.poll() is not None)
        assert process.poll() is None, (tmp_path/'verifier.log').read_text()
        yield assignment, executor, request, registry, runtime, tmp_path, journal
    finally:
        executor.stop(assignment['attempt_id'])
        remove_data(tmp_path)
        # The process belongs to this fixture. A root SIGTERM and exact runtime
        # removal do not touch any enrolled worker or shared service.
        subprocess.run(['sudo', '-n', '/usr/bin/pkill', '-TERM', '-f',
                        'livestack_node.workloads.launch_verifier --config '+runtime+'/config.json'],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait(timeout=5)
        log.close()
        subprocess.run(['sudo', '-n', '/usr/bin/rm', '-rf', '--', runtime], check=True)
        journal.close()


def launch(verifier, script, *, memory=512*1024**2, cpu=1, lease_file=None,
           rootless_native=False, isolation=None):
    assignment, executor, request, registry, _, root, _ = verifier
    path = root/'request.json'
    path.write_text(json.dumps(request))
    program = root/'launch.py'
    program.write_text(script)
    output = root/'out'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               TEST_REQUEST=str(path), TEST_REGISTRY=registry, TEST_ROOT=str(root))
    cwd, options = root, {}
    if isolation is not None:
        environment_root, source, other, view = isolation
        env.update(TEST_ENV_VIEW=str(view), TEST_ENV_HIDDEN=str(other/'secret'))
        cwd = view
        options = dict(inaccessible_paths=[str(environment_root)], bind_paths=[(str(source), str(view))])
    executor.start(assignment['attempt_id'], [sys.executable, str(program)], cwd, output,
                   env=env, cpu=cpu, memory_bytes=memory, max_seconds=60,
                   tasks=512 if rootless_native else 64, lease_file=lease_file,
                   rootless_docker=rootless_native, rootless_native=rootless_native,
                   native_host_address=os.environ.get('HARMONY_TEST_NATIVE_HOST_ADDRESS'), **options)
    return output


IMPORTS = '''import json,os,subprocess
from pathlib import Path
from livestack_node.workloads.launch_contract import verify_launch
request=json.loads(Path(os.environ['TEST_REQUEST']).read_text())
receipt=verify_launch(request,registry_path=os.environ['TEST_REGISTRY'])
'''


def test_real_admitted_rust_launch_positive_control(verifier):
    _, executor, _, _, _, root, _ = verifier
    source = root/'tiny.rs'
    source.write_text('fn main() { std::process::exit(17); }\n')
    output = launch(verifier, IMPORTS+'''
root=Path(os.environ['TEST_ROOT'])
subprocess.run([str(Path.home()/'.cargo/bin/rustc'),str(root/'tiny.rs'),'-o',str(root/'tiny')],check=True)
assert subprocess.run([str(root/'tiny')]).returncode==17
(root/'compiled.json').write_text(json.dumps(receipt))
''')
    result = until(lambda: executor.exit_result(output), 30)
    assert result['exit_code'] == 0, (output/'command.log').read_text()
    assert json.loads((root/'compiled.json').read_text())['host'] == 'physical-builder'


def test_real_admitted_private_docker_build_keeps_verification_native(verifier):
    _, executor, request, _, _, root, _ = verifier
    request['class'] = 'image'
    environment_root = root/'environments'
    source = environment_root/'handle-a'/'source'
    other = environment_root/'handle-b'/'source'
    view = root/'environment-view'
    source.mkdir(parents=True)
    other.mkdir(parents=True)
    view.mkdir()
    (source/'captured').write_text('current-task')
    (other/'secret').write_text('different-owner')
    context = root/'build-context'
    context.mkdir()
    (context/'marker').write_text('verified private builder')
    (context/'Dockerfile').write_text('FROM scratch\nCOPY marker /marker\n')
    output = launch(verifier, IMPORTS+'''
assert os.getuid()!=0, 'compiler frontend entered rootless user namespace'
root=Path(os.environ['TEST_ROOT'])
assert next(line for line in Path('/proc/self/status').read_text().splitlines()
            if line.startswith('NoNewPrivs:')).split()[1] == '1'
view=Path(os.environ['TEST_ENV_VIEW'])
assert (view/'captured').read_text() == 'current-task'
try: Path(os.environ['TEST_ENV_HIDDEN']).read_text()
except (PermissionError,FileNotFoundError): pass
else: raise AssertionError('sibling task environment was readable')
assert os.environ['DOCKER_HOST'].startswith('unix:///proc/')
assert subprocess.check_output(['docker','info','--format','{{.DockerRootDir}}'],text=True).strip()=='/run/harmony/data'
subprocess.run(['docker','build','--progress=plain','-t','tiny-proof',str(root/'build-context')],check=True)
digest=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}','tiny-proof'],text=True).strip()
assert digest.startswith('sha256:')
(root/'compiled.json').write_text(json.dumps(receipt))
''', rootless_native=True, memory=768*1024**2,
         isolation=(environment_root, source, other, view))
    result = until(lambda: executor.exit_result(output), 55)
    assert result['exit_code'] == 0, (output/'command.log').read_text()[-8000:]
    assert json.loads((root/'compiled.json').read_text())['host'] == 'physical-builder'


def test_copied_metadata_outside_attempt_refuses_before_compiler(verifier):
    assignment, executor, request, registry, _, root, _ = verifier
    executor.start(assignment['attempt_id'], ['/bin/sleep', '30'], root, root/'out',
                   env=dict(os.environ), cpu=.1, memory_bytes=64*1024**2)
    with pytest.raises(WorkloadError, match='compilation_peer_outside_attempt'):
        verify_launch(request, registry_path=registry)
    assert not (root/'compiled.json').exists()


@pytest.mark.parametrize('limits', [{'memory': 1024**3}, {'cpu': 2}])
def test_attempt_cgroup_cannot_exceed_authorized_execution_resources(verifier, limits):
    _, executor, _, _, _, root, _ = verifier
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n", **limits)
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_attempt_resource_limit_mismatch' in (output/'command.log').read_text()


@pytest.mark.parametrize('field,value', [
    ('host', 'foreign-host'), ('input_digest', 'f'*64), ('fence', 99),
    ('boot', 'foreign-boot'), ('policy_revision', 'foreign-revision'),
    ('worker', 'foreign-worker'), ('version', 2),
])
def test_forged_metadata_in_real_attempt_refuses(verifier, field, value):
    _, executor, request, _, _, root, _ = verifier
    request[field] = value
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_' in (output/'command.log').read_text()


def test_missing_verifier_and_untrusted_registry_refuse(verifier):
    _, _, request, registry, runtime, root, _ = verifier
    forged = root/'forged-registry.json'
    forged.write_text(Path(registry).read_text())
    with pytest.raises(WorkloadError, match='compilation_registry_untrusted'):
        verify_launch(request, registry_path=forged)
    subprocess.run(['sudo', '-n', '/usr/bin/mv', runtime+'/verify.sock', runtime+'/unavailable.sock'], check=True)
    with pytest.raises(WorkloadError, match='compilation_verification_unavailable'):
        verify_launch(request, registry_path=registry)


def test_root_registry_cannot_bless_fake_nonroot_socket(verifier):
    _, _, request, registry, _, root, _ = verifier
    endpoint = str(root/'fake.sock')
    # No fake response is needed: kernel server credentials reject the socket
    # before the client sends attempt metadata to it.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as fake:
        fake.bind(endpoint)
        fake.listen(1)
        subprocess.run(['sudo', '-n', sys.executable, '-B', '-c',
            'import json,sys;open(sys.argv[1],"w").write(json.dumps({"version":1,"slots":{"builder":sys.argv[2]}}))',
            registry, endpoint], check=True)
        with pytest.raises(WorkloadError, match='compilation_verifier_peer_untrusted'):
            verify_launch(request, registry_path=registry)


def test_copied_verifier_configuration_on_wrong_machine_cannot_start(verifier):
    _, _, _, _, runtime, _, _ = verifier
    change = '''import json,sys
from pathlib import Path
root=Path(sys.argv[1]);config=json.loads((root/'config.json').read_text())
config['machine_id']='0'*32;config['socket']=str(root/'wrong.sock')
path=root/'wrong-config.json';path.write_text(json.dumps(config));path.chmod(0o600)
'''
    subprocess.run(['sudo', '-n', sys.executable, '-B', '-c', change, runtime], check=True)
    result = subprocess.run(['sudo', '-n', '/usr/bin/env',
        'PYTHONPATH='+str(Path(__file__).resolve().parents[1]), sys.executable,
        '-m', 'livestack_node.workloads.launch_verifier', '--config', runtime+'/wrong-config.json'],
        capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'compilation_verifier_physical_machine_mismatch' in result.stderr
    assert not Path(runtime+'/wrong.sock').exists()


@pytest.mark.parametrize('mutation', ['cancel', 'revoke', 'expire'])
def test_lost_authority_grant_refuses_before_compiler(verifier, authority, mutation):
    assignment, executor, _, _, _, root, _ = verifier
    clients, _, _, value, write, _ = authority
    if mutation == 'cancel':
        clients['caller'].cancel(assignment['job_id'])
    elif mutation == 'revoke':
        value['hosts']['physical-builder'] = []
        write()
    else:
        value['expires'] = time.time()-1
        write()
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()


def test_oversized_real_authority_reply_refuses(verifier, authority, monkeypatch):
    _, executor, _, _, _, root, _ = verifier
    store = authority[0]['caller'].fixture_store
    original = store.verify_compilation
    def oversized(*args, **kwargs):
        return dict(original(*args, **kwargs), padding='x'*16384)
    monkeypatch.setattr(store, 'verify_compilation', oversized)
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_authority_response_oversized' in (output/'command.log').read_text()


def test_verified_launch_receipt_preserves_decision_id(verifier):
    assignment, executor, _, _, _, root, _ = verifier
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'receipt.json').write_text(json.dumps(receipt))\n")
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] == 0, (output/'command.log').read_text()
    assert json.loads((root/'receipt.json').read_text())['decision_id'] == assignment['decision_id']


def test_mismatched_decision_id_refuses_before_compiler(verifier, authority, monkeypatch):
    _, executor, _, _, _, root, _ = verifier
    store = authority[0]['caller'].fixture_store
    original = store.verify_compilation
    def mismatched(*args, **kwargs):
        return dict(original(*args, **kwargs), decision_id='01J00000000000000000000000')
    monkeypatch.setattr(store, 'verify_compilation', mismatched)
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] != 0
    assert not (root/'compiler-started').exists()
    assert 'compilation_authority_receipt_mismatch' in (output/'command.log').read_text()


def test_verification_wall_deadline_refuses_slow_authority(verifier, authority, monkeypatch):
    _, executor, _, _, _, root, _ = verifier
    store = authority[0]['caller'].fixture_store
    original = store.verify_compilation
    def delayed(*args, **kwargs):
        receipt = original(*args, **kwargs)
        time.sleep(8)
        return receipt
    monkeypatch.setattr(store, 'verify_compilation', delayed)
    started = time.monotonic()
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('bad')\n")
    result = until(lambda: executor.exit_result(output), 7)
    assert result['exit_code'] != 0
    assert time.monotonic()-started < 6
    assert not (root/'compiler-started').exists()


def test_dropped_authority_connection_is_retried_within_the_deadline(verifier, authority):
    # xc-win-1-wsl, 2026-10-02: the verifier's one authority request crossed a
    # 230 ms / ~10%-loss path; one lost connection refused the launch
    # (compilation_verification_unavailable) and threw away a multi-minute e2e
    # attempt after ten good launches. The authority drops a connection here the
    # way its own BoundedRequests does at its connection bound.
    _, executor, _, _, _, root, _ = verifier
    server = authority[0]['caller'].fixture_server
    original, dropped = server.process_request, []
    def drop_first(request, client_address):
        if not dropped:
            dropped.append(client_address)
            return server.shutdown_request(request)
        return original(request, client_address)
    server.process_request = drop_first
    output = launch(verifier, IMPORTS+"Path(os.environ['TEST_ROOT'],'compiler-started').write_text('ok')\n")
    result = until(lambda: executor.exit_result(output), 10)
    assert dropped
    assert result['exit_code'] == 0, (output/'command.log').read_text()
    assert (root/'compiler-started').exists()
    # stderr (the journal under systemd) names the failure, not just the file.
    assert 'compilation_authority_transport_retry: failure=' in (root/'verifier.log').read_text()


def test_current_receipt_is_bounded_and_refusal_replaces_success(verifier):
    _, executor, _, _, _, root, _ = verifier
    output = launch(verifier, '''import json,os
from pathlib import Path
from livestack_node.workloads.launch_guard import require_compilation
from livestack_node.workloads.model import WorkloadError
request=json.loads(Path(os.environ['TEST_REQUEST']).read_text())
names={'worker':'HARMONY_WORKER','host':'HARMONY_PHYSICAL_HOST','policy_revision':'HARMONY_POLICY_REVISION',
       'boot':'HARMONY_BOOT','job_id':'HARMONY_JOB','attempt_id':'HARMONY_ATTEMPT','fence':'HARMONY_FENCE','input_digest':'HARMONY_INPUT_DIGEST'}
os.environ.update({variable:str(request[key]) for key,variable in names.items()})
os.environ['HARMONY_OUTPUT']=str(Path(os.environ['TEST_ROOT'])/'out')
registry=os.environ['TEST_REGISTRY']
for _ in range(3): require_compilation('rust',registry_path=registry)
path=Path(os.environ['HARMONY_OUTPUT'])/'compilation-rust.json'
assert json.loads(path.read_text())['admitted'] is True
os.environ['HARMONY_FENCE']='999'
try:
    require_compilation('rust',registry_path=registry)
except WorkloadError:
    pass
else:
    raise AssertionError('stale fence granted')
assert path.stat().st_size <= 16384
assert json.loads(path.read_text())['admitted'] is False
assert len(list(path.parent.glob('compilation-*.json'))) == 1
assert not list(path.parent.glob('compilation-*.tmp'))
''')
    result = until(lambda: executor.exit_result(output))
    assert result['exit_code'] == 0, (output/'command.log').read_text()


@pytest.mark.parametrize('authority', [{'lease_seconds': 3}], indirect=True)
@pytest.mark.parametrize('ending', ['expiry', 'cancel'])
def test_lost_authorization_stops_real_compiler_grandchild_before_capacity_reuse(verifier, authority, ending):
    assignment, executor, _, _, _, root, _ = verifier
    clients, submit, register, *_ = authority
    child = root/'compiler-parent.py'
    child.write_text('''import json,os,subprocess,time
from pathlib import Path
root=Path(os.environ['TEST_ROOT'])
compiler=subprocess.Popen([str(Path.home()/'.cargo/bin/rustc'),'-','--crate-type=lib','--emit=metadata','-o',str(root/'tiny.rmeta')],stdin=subprocess.PIPE)
(root/'compiler-pids.json').write_text(json.dumps([os.getpid(),compiler.pid]))
time.sleep(120)
''')
    keeper = LeaseKeeper(clients['builder'], assignment, root/'lease', interval=1).start()
    # Simulate the worker disappearing after a genuine authenticated renewal:
    # no fake clock/lease store, and the running wrapper still owns expiry.
    if ending == 'expiry':
        keeper.stopped.set()
    try:
        launch(verifier, IMPORTS+'''
root=Path(os.environ['TEST_ROOT'])
subprocess.Popen([__import__('sys').executable,str(root/'compiler-parent.py')])
__import__('time').sleep(120)
''', lease_file=root/'lease')
        pids = until(lambda: json.loads((root/'compiler-pids.json').read_text())
                     if (root/'compiler-pids.json').exists() else None, 2)
        until(lambda: 'rustc' in os.readlink('/proc/'+str(pids[1])+'/exe'), 2)
        if ending == 'cancel':
            clients['caller'].cancel(assignment['job_id'])
        until(lambda: not executor.alive(assignment['attempt_id']), 6)
        for pid in pids:
            assert not Path('/proc', str(pid)).exists(), 'owned compiler descendants survived lease expiry'
        terminal = 'queued' if ending == 'expiry' else 'cancelled'
        until(lambda: clients['caller'].get(assignment['job_id'])['state'] == terminal, 6)
        register('builder-alias')
        submit('next')
        assert claim(clients, 'builder-alias') is None, 'capacity reused before cleanup acknowledgment'
        executor.stop(assignment['attempt_id'])
        clients['builder'].request('worker/report', dict(boot='boot',
            cleaned=[assignment['attempt_id']], report=dict(
                capacity={'cpu': 1, 'memory_bytes': 67108864},
                available={'cpu': 1, 'memory_bytes': 67108864},
                labels={}, handlers=['build.v1','ui.v1'], ready=True)))
        assert claim(clients, 'builder-alias') is not None
    finally:
        keeper.close()


@pytest.mark.parametrize('authority', [{'lease_seconds': 15}], indirect=True)
def test_private_image_builder_descendant_stops_on_lease_expiry(verifier, authority):
    assignment, executor, request, _, _, root, _ = verifier
    request['class'] = 'image'
    # Consume installed prebuilt system artifacts; this scratch image needs no
    # network pull and does not compile a helper before admission.
    context = root/'build-context'
    context.mkdir()
    shutil.copy2('/bin/sleep', context/'sleep')
    # Resolve this host's installed runtime instead of guessing its libc,
    # architecture or optional sleep dependencies (Ubuntu uses libselinux).
    env={key:value for key,value in os.environ.items() if not key.startswith('LD_')}
    dependencies=subprocess.check_output(['ldd','/bin/sleep'],env=env,timeout=5)
    assert len(dependencies)<=16384, 'sleep dependency report exceeds bound'
    text=dependencies.decode()
    assert 'not found' not in text, text
    libraries=set(re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)\s+\(',text,re.MULTILINE))
    assert 1<=len(libraries)<=32, 'sleep runtime dependency count outside bound'
    total=0
    for library in sorted(libraries):
        source=Path(library)
        assert len(library)<=4096 and source.is_file()
        total+=source.stat().st_size
        assert total<=64*1024**2, 'sleep runtime dependency bytes exceed bound'
        target=context/library.lstrip('/')
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    (context/'Dockerfile').write_text('FROM scratch\nCOPY . /\nRUN ["/sleep", "120"]\n')
    clients = authority[0]
    keeper = LeaseKeeper(clients['builder'], assignment, root/'lease', interval=1).start()
    keeper.stopped.set()
    try:
        output = launch(verifier, IMPORTS+'''
root=Path(os.environ['TEST_ROOT'])
subprocess.run(['docker','build','--no-cache','--progress=plain',str(root/'build-context')],check=True)
''', rootless_native=True, memory=768*1024**2, lease_file=root/'lease')
        group = until(lambda: executor.inspect(assignment['attempt_id']).get('ControlGroup'))
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        def builder_sleep():
            result = executor.exit_result(output)
            if result is not None:
                raise AssertionError('builder stopped before the live descendant control: '+
                                     (output/'command.log').read_text()[-4000:])
            for processes in cgroup.rglob('cgroup.procs'):
                try:
                    pids = processes.read_text().split()
                except FileNotFoundError:
                    continue
                for pid in pids:
                    try:
                        with Path('/proc', pid, 'cmdline').open('rb') as command:
                            args = command.read(4096).split(b'\0')
                        if args[:2] == [b'/sleep', b'120']:
                            return int(pid)
                    except FileNotFoundError:
                        continue
            return None
        pid = until(builder_sleep, 10)
        until(lambda: not executor.alive(assignment['attempt_id']), 20)
        executor.stop(assignment['attempt_id'])
        until(lambda: not Path('/proc', str(pid)).exists(), 3)
        assert not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text()
        until(lambda: clients['caller'].get(assignment['job_id'])['state'] == 'queued', 3)
        _, submit, register, *_ = authority
        register('builder-alias')
        submit('next-image')
        assert claim(clients, 'builder-alias') is None, 'builder capacity reused before cleanup acknowledgment'
        clients['builder'].request('worker/report', dict(boot='boot',
            cleaned=[assignment['attempt_id']], report=dict(
                capacity={'cpu': 1, 'memory_bytes': 1024**3},
                available={'cpu': 1, 'memory_bytes': 1024**3},
                labels={}, handlers=['build.v1','ui.v1'], ready=True)))
        assert claim(clients, 'builder-alias') is not None
    finally:
        keeper.close()


@pytest.mark.parametrize('classes,admitted', [(['rust', 'image'], True), (['rust', 'native'], False)])
def test_multi_class_launch_uses_one_live_check_and_refuses_missing_class(verifier, authority, monkeypatch, classes, admitted):
    _, executor, _, _, _, root, _ = verifier
    store = authority[0]['caller'].fixture_store
    original = store.verify_compilation
    calls = []
    def observe(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'verify_compilation', observe)
    program = """import json,os,subprocess
from pathlib import Path
from livestack_node.workloads.launch_guard import require_compilations
request=json.loads(Path(os.environ['TEST_REQUEST']).read_text())
names={'worker':'HARMONY_WORKER','host':'HARMONY_PHYSICAL_HOST','policy_revision':'HARMONY_POLICY_REVISION',
       'boot':'HARMONY_BOOT','job_id':'HARMONY_JOB','attempt_id':'HARMONY_ATTEMPT','fence':'HARMONY_FENCE','input_digest':'HARMONY_INPUT_DIGEST'}
os.environ.update({variable:str(request[key]) for key,variable in names.items()})
root=Path(os.environ['TEST_ROOT'])
os.environ['HARMONY_OUTPUT']=str(root/'out')
"""
    program += 'require_compilations('+repr(classes)+",registry_path=os.environ['TEST_REGISTRY'])\n"
    program += "(root/'tiny.rs').write_text('fn main() {}')\n"
    program += "subprocess.run([str(Path.home()/'.cargo/bin/rustc'),str(root/'tiny.rs'),'-o',str(root/'tiny')],check=True)\n"
    program += "subprocess.run([str(root/'tiny')],check=True)\n(root/'compiler-started').write_text('authorized')\n"
    output = launch(verifier, program)
    result = until(lambda: executor.exit_result(output))
    assert (result['exit_code'] == 0) is admitted, (output/'command.log').read_text()
    assert len(calls) == 1, 'one spawn must issue exactly one live authority check'
    assert (root/'compiler-started').exists() is admitted
    for item in classes:
        receipt = output/('compilation-'+item+'.json')
        assert receipt.stat().st_size <= 16384
        assert json.loads(receipt.read_text())['admitted'] is admitted
    if not admitted:
        assert 'compilation_class_not_reserved' in (output/'command.log').read_text()
