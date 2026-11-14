"""One default per (handler, platform): an x86_64 and an aarch64 release of one handler serve one job set
(openspec handler-platform-defaults). Real HTTP/SQLite authority; worker sync on a real package store."""
import hashlib
import json
import sys

import pytest

from livestack_node.workloads.handler_installer import HandlerPackageStore
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.transfer import InputTransfer

from test_workload_handler_registry import activate, authority, package, stage  # noqa: F401 (fixture)
from test_workload_handler_platform_skip import _worker


def package_for(tmp_path, version, architecture):
    manifest, _digest, archive, archive_digest = package(tmp_path, version)
    manifest['architecture'] = architecture
    return manifest, validate_manifest(manifest)['release_digest'], archive, archive_digest


def identity(bundle):
    manifest, digest = bundle[0], bundle[1]
    return dict(handler_id='test.v1', release_digest=digest, execution_contract=1,
                payload_schema=manifest['payload_schema'], result_schema=manifest['result_schema'])


def report(*bundles):
    releases = sorted((identity(b) for b in bundles), key=lambda item: item['release_digest'])
    return dict(capacity={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3},
                available={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3}, labels={},
                handlers=['test.v1'], ready=True,
                handler_inventory={'generation': 2, 'defaults': {'test.v1': bundles[0][1]}, 'releases': releases})


def submit(clients, tmp_path, key):
    source = key.encode()
    path = tmp_path/key
    path.write_bytes(source)
    InputTransfer(clients['caller']).put(path)
    return clients['caller'].submit(dict(version=1, key=key, handler='test.v1',
        input_digest=hashlib.sha256(source).hexdigest(), need={'cpu': 1, 'memory_bytes': 128*1024**2},
        handler_release={'selection': 'default'})), hashlib.sha256(source).hexdigest()


def complete(client, boot, assignment, input_digest, release):
    return client.request('worker/complete', dict(boot=boot, attempt_id=assignment['attempt_id'],
        fence=assignment['fence'], input_digest=input_digest, outcome='succeeded',
        result={'exit_code': 0, 'resources': {'oom_kill': 0, 'pids_max_events': 0}, 'artifacts': []},
        handler_result_identity=dict(job_id=assignment['job_id'], attempt_id=assignment['attempt_id'],
            fence=assignment['fence'], worker=assignment['worker'], boot=assignment['boot'],
            input_digest=input_digest, handler_release=release)))


def test_a_foreign_platform_activation_adds_a_platform_default_beside_the_primary(authority, tmp_path):
    _server, clients = authority
    x86, arm, x86_next = (package_for(tmp_path, 'x', 'x86_64'), package_for(tmp_path, 'a', 'aarch64'),
                          package_for(tmp_path, 'x2', 'x86_64'))
    for bundle in (x86, arm, x86_next):
        stage(clients['operator'], bundle)
    activate(clients['operator'], 'test.v1', x86[1], 0, '1'*32)
    receipt = activate(clients['operator'], 'test.v1', arm[1], 1, '2'*32)
    assert receipt['previous_digest'] is None
    linux = x86[0]['platform']
    assert clients['operator'].request('handler-releases/status')['defaults'] == {
        'test.v1': x86[1], f'test.v1@{linux}/aarch64': arm[1]}
    # A same-platform activation replaces the primary and leaves the other platform's default alone.
    assert activate(clients['operator'], 'test.v1', x86_next[1], 2, '3'*32)['previous_digest'] == x86[1]
    status = clients['operator'].request('handler-releases/status')
    assert status['defaults'] == {'test.v1': x86_next[1], f'test.v1@{linux}/aarch64': arm[1]}
    assert {r['release_digest'] for r in status['releases'] if r['is_default']} == {x86_next[1], arm[1]}


def test_one_default_job_set_runs_on_each_platform_pinned_to_that_platforms_release(authority, tmp_path):
    _server, clients = authority
    x86, arm, arm_next = (package_for(tmp_path, 'x', 'x86_64'), package_for(tmp_path, 'a', 'aarch64'),
                          package_for(tmp_path, 'a2', 'aarch64'))
    for bundle in (x86, arm, arm_next):
        stage(clients['operator'], bundle)
    activate(clients['operator'], 'test.v1', x86[1], 0, '1'*32)
    activate(clients['operator'], 'test.v1', arm[1], 1, '2'*32)
    job_a, input_a = submit(clients, tmp_path, 'job-a')
    job_b, input_b = submit(clients, tmp_path, 'job-b')
    assert job_a['spec']['handler_release']['release_digest'] == x86[1]
    assert [v['release_digest'] for v in job_a['spec']['handler_release_variants']] == [arm[1]]
    # Activating a newer aarch64 default after acceptance changes nothing for the accepted jobs.
    activate(clients['operator'], 'test.v1', arm_next[1], 2, '3'*32)

    # worker-b advertises only the NEW aarch64 release: neither accepted job may run there.
    clients['worker-b'].request('worker/report', dict(boot='boot-b', report=report(arm_next)))
    assert clients['worker-b'].request('worker/claim', {'boot': 'boot-b'})['assignment'] is None
    # worker-b with the accepted aarch64 release: pinned to it, and completes against it.
    clients['worker-b'].request('worker/report', dict(boot='boot-b', report=report(arm)))
    on_arm = clients['worker-b'].request('worker/claim', {'boot': 'boot-b'})['assignment']
    assert on_arm['handler_release']['release_digest'] == arm[1]
    with pytest.raises(WorkloadError, match='handler_result_release_identity_mismatch'):
        complete(clients['worker-b'], 'boot-b', on_arm, input_a if on_arm['job_id'] == job_a['id'] else input_b,
                 identity(x86))
    complete(clients['worker-b'], 'boot-b', on_arm, input_a if on_arm['job_id'] == job_a['id'] else input_b,
             identity(arm))
    # worker (x86_64) takes the other job, pinned to the primary.
    clients['worker'].request('worker/report', dict(boot='boot-1', report=report(x86)))
    on_x86 = clients['worker'].request('worker/claim', {'boot': 'boot-1'})['assignment']
    assert on_x86['job_id'] != on_arm['job_id']
    assert on_x86['handler_release']['release_digest'] == x86[1]
    complete(clients['worker'], 'boot-1', on_x86, input_a if on_x86['job_id'] == job_a['id'] else input_b,
             identity(x86))
    for job in (job_a, job_b):
        assert clients['caller'].get(job['id'])['state'] == 'succeeded'


def _sync(server, *bundles):
    registry = server.store.handler_registry
    desired = registry.desired({'test.v1'})
    by_digest = {b[1]: b for b in bundles}
    for item in desired['releases']:
        bundle = by_digest[item['release_digest']]
        assert item['archive_digest'] == bundle[3]
    return desired


@pytest.mark.parametrize('architecture', ['x86_64', 'aarch64'])
def test_each_worker_adopts_its_own_platforms_default_from_one_sync(authority, tmp_path, architecture):
    server, clients = authority
    x86, arm = package_for(tmp_path, 'x', 'x86_64'), package_for(tmp_path, 'a', 'aarch64')
    for bundle in (x86, arm):
        stage(clients['operator'], bundle)
    activate(clients['operator'], 'test.v1', x86[1], 0, '1'*32)
    activate(clients['operator'], 'test.v1', arm[1], 1, '2'*32)
    sync = _sync(server, x86, arm)
    mine, other = (x86, arm) if architecture == 'x86_64' else (arm, x86)
    worker = _worker(tmp_path, x86[0]['platform'], architecture, required=['test.v1'])
    archive = mine[2]
    worker.transfer = type('T', (), {'get': staticmethod(lambda _d, dest: dest.write_bytes(archive.read_bytes()))})()

    assert worker._sync_handler_registry(sync) is True

    assert worker.handler_registry_failures == []  # required, and served here: the sibling is not "missing"
    assert [s['release_digest'] for s in worker.handler_platform_skips] == [other[1]]
    assert worker.handler_registry_state == {'generation': 2, 'defaults': {'test.v1': mine[1]}}
    assert worker.handler_inventory['defaults'] == {'test.v1': mine[1]}
    assert not (tmp_path/'worker-state'/other[1]).exists()


def test_an_x86_worker_from_before_this_change_ignores_the_variant(authority, tmp_path):
    """The sync an older worker receives: it skips the aarch64 descriptor by name, drops its key, keeps x86."""
    server, clients = authority
    x86, arm = package_for(tmp_path, 'x', 'x86_64'), package_for(tmp_path, 'a', 'aarch64')
    for bundle in (x86, arm):
        stage(clients['operator'], bundle)
    activate(clients['operator'], 'test.v1', x86[1], 0, '1'*32)
    activate(clients['operator'], 'test.v1', arm[1], 1, '2'*32)
    sync = _sync(server, x86, arm)
    defaults = dict(sync['defaults'])
    skipped = {arm[1]}  # what the platform-skip worker computes for a foreign descriptor
    for handler in list(defaults):  # the pre-change worker loop, verbatim
        if defaults[handler] in skipped:
            del defaults[handler]
    assert defaults == {'test.v1': x86[1]}
    assert json.dumps(sync)  # bounded, encodable

