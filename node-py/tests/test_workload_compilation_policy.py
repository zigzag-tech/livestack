"""Disposable authenticated authority with real SQLite and policy files."""
import hashlib
from dataclasses import replace
from io import BytesIO
import json
from threading import Thread
import time

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.compilation_policy import CompilationPolicy
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.model import Limits


@pytest.fixture
def authority(tmp_path, request):
    policy_path = tmp_path/'policy.json'
    value = dict(version=1, revision='revision-1', expires=time.time()+3600,
                 hosts={'physical-ui': [], 'physical-builder': ['rust', 'image']},
                 enrollments={'ui': 'physical-ui', 'ui-alias': 'physical-ui',
                              'builder': 'physical-builder', 'builder-alias': 'physical-builder'})
    def write():
        policy_path.write_text(json.dumps(value))
        policy_path.chmod(0o600)
    write()
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'build.v1', 'ui.v1'},
        limits=Limits(**getattr(request, 'param', {})),
        compilation_policy=CompilationPolicy(policy_path, {'build.v1': ['rust', 'image']}))
    principals = [Principal('caller', 'c'*32, 'caller', ('build.v1', 'ui.v1'))]
    principals += [Principal(host, str(index)*32, 'worker', worker=host, host=host)
                   for index, host in enumerate(value['enrollments'], 1)]
    server = WorkloadServer(('127.0.0.1', 0), store, principals)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    clients = {p.id: WorkloadClient(url, p.token) for p in principals}
    # Fault injection changes the real authority's reply after authenticating
    # and validating its real SQLite attempt; no emulated grant store is used.
    clients['caller'].fixture_store = store
    clients['caller'].fixture_server = server
    data = b'compilation source fixture'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('caller', digest, len(data), BytesIO(data))
    def submit(key, handler='build.v1'):
        return clients['caller'].request('jobs', dict(version=1, key=key, handler=handler,
            input_digest=digest, need={'cpu': 1, 'memory_bytes': 768*1024**2}))
    def register(host):
        return clients[host].request('worker/report', dict(boot='boot', report=dict(
            capacity={'cpu': 1, 'memory_bytes': 1024**3},
            available={'cpu': 1, 'memory_bytes': 1024**3},
            labels={'installed-rust': 'yes', 'installed-docker': 'yes'},
            handlers=['build.v1', 'ui.v1'], ready=True)))
    yield clients, submit, register, value, write, policy_path
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def claim(clients, host):
    return clients[host].request('worker/claim', {'boot': 'boot'})['assignment']


def verify(clients, host, assignment, **changes):
    body = dict(boot='boot', attempt_id=assignment['attempt_id'], fence=assignment['fence'],
                input_digest=assignment['spec']['input_digest'], **{'class': 'rust'})
    body.update(changes)
    return clients[host].request('worker/verify-compilation', body)


def test_ui_host_aliases_refuse_build_but_execute_prebuilt_runtime(authority):
    clients, submit, register, *_ = authority
    build = submit('build')
    for host in ('ui', 'ui-alias'):
        register(host)
        assert claim(clients, host) is None
    reason = clients['caller'].request('jobs/'+build['id'])['reason']
    assert 'compilation_not_admitted' in reason
    runtime = submit('runtime', 'ui.v1')
    assignment = claim(clients, 'ui')
    assert assignment['job_id'] == runtime['id']
    assert assignment['compilation'] is None
    with pytest.raises(WorkloadError) as error:
        verify(clients, 'ui', assignment)
    assert b'compilation_class_not_reserved' in str(error.value).encode()


def test_builder_positive_control_and_fenced_identity(authority):
    clients, submit, register, *_ = authority
    register('builder')
    submit('build')
    assignment = claim(clients, 'builder')
    receipt = verify(clients, 'builder', assignment)
    assert receipt['host'] == 'physical-builder'
    assert receipt['policy_revision'] == 'revision-1'
    assert receipt['resources']['cpu'] == 1
    for changes, reason in [({'input_digest': 'f'*64}, b'compilation_input_mismatch'),
                            ({'fence': assignment['fence']+1}, b'compilation_attempt_not_live'),
                            ({'boot': 'foreign'}, b'worker session is not current'),
                            ({'class': 'flutter'}, b'compilation_class_not_reserved')]:
        with pytest.raises(WorkloadError) as error:
            verify(clients, 'builder', assignment, **changes)
        assert reason in str(error.value).encode()
    register('builder-alias')
    with pytest.raises(WorkloadError):
        verify(clients, 'builder-alias', assignment)


def test_aliases_charge_one_physical_budget(authority):
    clients, submit, register, *_ = authority
    for host in ('builder', 'builder-alias'):
        register(host)
    submit('first')
    submit('second')
    assert claim(clients, 'builder') is not None
    assert claim(clients, 'builder-alias') is None


def test_draining_builder_keeps_live_compilation_verification(authority):
    clients, submit, register, *_ = authority
    register('builder')
    submit('build')
    assignment = claim(clients, 'builder')
    server = clients['caller'].fixture_server
    server.replace_principals([replace(p, claim_enabled=False) if p.id == 'builder' else p
                               for p in server.principals])
    assert clients['builder'].request('worker/claim', {'boot': 'boot'}) == {
        'assignment': None, 'reason': 'worker_draining'}
    assert verify(clients, 'builder', assignment)['policy_revision'] == 'revision-1'
    assert clients['builder'].request('worker/heartbeat', dict(boot='boot',
        attempt_id=assignment['attempt_id'], fence=assignment['fence']))['lease_remaining'] > 0


def test_forbidden_ui_worker_is_not_a_retry_alternative(authority):
    clients, submit, register, *_ = authority
    register('builder')
    register('ui')
    submit('retry')
    assignment = claim(clients, 'builder')
    clients['builder'].request('worker/complete', dict(boot='boot',
        attempt_id=assignment['attempt_id'], fence=assignment['fence'],
        input_digest=assignment['spec']['input_digest'], outcome='infrastructure',
        result={'error': 'disposable_failure'}))
    assert claim(clients, 'builder') is not None


@pytest.mark.parametrize('mutation,reason', [
    ('expire', b'compilation_policy_expired'),
    ('revoke', b'compilation_not_admitted'),
    ('missing', b'compilation_policy_unavailable'),
    ('oversized', b'compilation_policy_untrusted'),
    ('unsupported', b'compilation_policy_unsupported'),
])
def test_policy_changes_refuse_launch_and_renewal(authority, mutation, reason):
    clients, submit, register, value, write, path = authority
    register('builder')
    submit('build')
    assignment = claim(clients, 'builder')
    assert verify(clients, 'builder', assignment)['version'] == 1
    if mutation == 'expire':
        value['expires'] = time.time()-1
    elif mutation == 'revoke':
        value['hosts']['physical-builder'] = []
    elif mutation == 'unsupported':
        value['version'] = 2
    write()
    if mutation == 'missing':
        path.unlink()
    elif mutation == 'oversized':
        path.write_bytes(b' '*16385)
    with pytest.raises(WorkloadError) as error:
        verify(clients, 'builder', assignment)
    assert reason in str(error.value).encode()
    with pytest.raises(WorkloadError) as error:
        clients['builder'].request('worker/heartbeat', dict(boot='boot',
            attempt_id=assignment['attempt_id'], fence=assignment['fence']))
    assert reason in str(error.value).encode()


def test_authenticated_completed_job_retains_original_compilation_producer(authority):
    clients, submit, register, value, write, _ = authority
    register('builder')
    job = submit('artifact-producer')
    assignment = claim(clients, 'builder')
    current = clients['caller'].get(job['id'])
    attempt = current['attempts'][0]
    assert attempt['id'] == assignment['attempt_id']
    assert attempt['worker'] == assignment['worker']
    assert attempt['boot'] == assignment['boot']
    assert attempt['fence'] == assignment['fence']
    assert attempt['compilation'] == assignment['compilation']
    clients['builder'].request('worker/complete', dict(boot='boot',
        attempt_id=assignment['attempt_id'], fence=assignment['fence'],
        input_digest=assignment['spec']['input_digest'], outcome='succeeded',
        result={'exit_code': 0, 'artifacts': []}))
    value['revision'] = 'revision-2'
    value['hosts']['physical-builder'] = []
    write()
    completed = clients['caller'].get(job['id'])
    assert completed['state'] == 'succeeded'
    assert completed['attempts'][0]['state'] == 'ended'
    assert completed['attempts'][0]['compilation'] == assignment['compilation']
    assert completed['attempts'][0]['compilation']['policy_revision'] == 'revision-1'
    with pytest.raises(WorkloadError):
        verify(clients, 'builder', assignment)
    server = clients['caller'].fixture_server
    server.replace_principals([replace(p, claim_enabled=False) if p.id == 'builder' else p
                               for p in server.principals])
    runtime = submit('artifact-runtime', 'ui.v1')
    register('ui')
    runtime_assignment = claim(clients, 'ui')
    assert runtime_assignment['job_id'] == runtime['id']
    assert clients['caller'].get(runtime['id'])['attempts'][0]['compilation'] is None


def test_apple_class_granted_only_by_policy(tmp_path):
    """openspec/changes/apple-host-compilation: an Apple handler advertised by a
    host whose policy lacks `apple` is refused by name; the Mac is admitted."""
    policy_path = tmp_path/'policy.json'
    policy_path.write_text(json.dumps(dict(version=1, revision='apple-1', expires=time.time()+3600,
        hosts={'linux-builder': ['rust', 'native'], 'mac': ['apple', 'rust', 'native']},
        enrollments={'linux-builder': 'linux-builder', 'mac': 'mac'})))
    policy_path.chmod(0o600)
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'apple.v1'}, limits=Limits(),
        compilation_policy=CompilationPolicy(policy_path, {'apple.v1': ['apple', 'rust', 'native']}))
    principals = [Principal('caller', 'c'*32, 'caller', ('apple.v1',)),
                  Principal('linux-builder', '1'*32, 'worker', worker='linux-builder', host='linux-builder'),
                  Principal('mac', '2'*32, 'worker', worker='mac', host='mac')]
    server = WorkloadServer(('127.0.0.1', 0), store, principals)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        clients = {p.id: WorkloadClient(url, p.token) for p in principals}
        data = b'apple source fixture'
        digest = hashlib.sha256(data).hexdigest()
        server.blobs.put('caller', digest, len(data), BytesIO(data))
        job = clients['caller'].request('jobs', dict(version=1, key='apple', handler='apple.v1',
            input_digest=digest, need={'cpu': 1, 'memory_bytes': 768*1024**2}))
        resources = {'cpu': 1, 'memory_bytes': 1024**3}
        clients['linux-builder'].request('worker/report', dict(boot='boot', report=dict(
            capacity=resources, available=resources, labels={}, handlers=['apple.v1'], ready=True)))
        assert claim(clients, 'linux-builder') is None
        assert 'compilation_not_admitted: operator host policy' in clients['caller'].request('jobs/'+job['id'])['reason']
        clients['mac'].request('worker/report', dict(boot='boot', report=dict(
            capacity=resources, available=resources, labels={}, handlers=['apple.v1'], ready=True)))
        assignment = claim(clients, 'mac')
        assert assignment['job_id'] == job['id']
        assert assignment['compilation']['classes'] == ['apple', 'native', 'rust']
        receipt = verify(clients, 'mac', assignment, **{'class': 'apple'})
        assert receipt['host'] == 'mac' and 'apple' in receipt['classes']
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def renew(clients, assignment):
    return clients['builder'].request('worker/heartbeat', dict(boot='boot',
        attempt_id=assignment['attempt_id'], fence=assignment['fence']))


@pytest.mark.parametrize('change', ['revision-only', 'widen-class', 'widen-host'])
def test_widening_policy_revision_preserves_admitted_attempt(authority, change):
    """A new revision that still grants every admitted class keeps the attempt;
    its receipt keeps the admitting revision. Fails on the old exact-receipt
    comparison (compilation_policy_revision_changed)."""
    clients, submit, register, value, write, _ = authority
    register('builder')
    submit('build')
    assignment = claim(clients, 'builder')
    value['revision'] = 'revision-2'
    if change == 'widen-class':
        value['hosts']['physical-builder'] = ['rust', 'image', 'native']
    elif change == 'widen-host':
        value['hosts']['physical-ui'] = ['rust']
    write()
    assert renew(clients, assignment)['lease_remaining'] > 0
    receipt = verify(clients, 'builder', assignment)
    assert receipt['policy_revision'] == 'revision-1'
    assert receipt['classes'] == ['image', 'rust']


@pytest.mark.parametrize('change', ['narrow-class', 'remove-host'])
def test_narrowing_policy_revision_revokes_admitted_attempt(authority, change):
    clients, submit, register, value, write, _ = authority
    register('builder')
    submit('build')
    assignment = claim(clients, 'builder')
    value['revision'] = 'revision-2'
    if change == 'narrow-class':
        value['hosts']['physical-builder'] = ['rust']
    else:
        del value['hosts']['physical-builder']
        value['enrollments'] = {k: v for k, v in value['enrollments'].items() if v != 'physical-builder'}
    write()
    for call in (lambda: renew(clients, assignment), lambda: verify(clients, 'builder', assignment)):
        with pytest.raises(WorkloadError) as error:
            call()
        assert b'compilation_not_admitted' in str(error.value).encode()
