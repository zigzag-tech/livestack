"""Real HTTP/SQLite coverage for handler release staging and exact assignment."""
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.handler_installer import HandlerPackageStore
from livestack_node.workloads.handler_release_cli import main as release_cli_main
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer


FIXTURE = Path(__file__).parent/'fixtures'/'harmony-handler-release-v1'
RUNTIMES = {'node22': '/usr/bin/python3'}


def package(tmp_path, version='a'):
    manifest = json.loads((FIXTURE/'manifest.json').read_text())
    manifest['handler_id'] = 'test.v1'
    manifest['release_version'] = version
    checked = validate_manifest(manifest)
    entry = manifest['files'][0]
    archive_path = tmp_path/f'{version}.tar'
    with tarfile.open(archive_path, 'w', format=tarfile.USTAR_FORMAT) as archive:
        content = (FIXTURE/entry['path']).read_bytes()
        info = tarfile.TarInfo(entry['path'])
        info.mode, info.size, info.mtime, info.uid, info.gid = entry['mode'], len(content), 0, 0, 0
        info.uname = info.gname = ''
        archive.addfile(info, io.BytesIO(content))
    archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return manifest, checked['release_digest'], archive_path, archive_digest


@pytest.fixture
def authority(tmp_path):
    handlers = {'test.v1'}
    store = WorkloadStore(tmp_path/'authority'/'jobs.db', handlers=handlers)
    principals = [
        Principal('operator', 'o'*32, 'admin', ('test.v1',)),
        Principal('caller', 'c'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='host1'),
        Principal('worker-b', 'x'*32, 'worker', worker='w2', host='host2'),
    ]
    policy = {'revision': 'policy-1', 'retention_seconds': 24*60*60, 'handlers': {'test.v1': {
        'runtime_ids': ['node22'], 'backends': ['native']}}}
    server = WorkloadServer(('127.0.0.1', 0), store, principals, handler_release_policy=policy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients = {name: WorkloadClient(f'http://127.0.0.1:{server.server_port}', token)
               for name, token in (('operator', 'o'*32), ('caller', 'c'*32),
                                   ('worker', 'w'*32), ('worker-b', 'x'*32))}
    try:
        yield server, clients
    finally:
        for client in clients.values():
            client.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def stage(client, bundle):
    manifest, digest, archive, archive_digest = bundle
    uploaded = InputTransfer(client).put(archive)
    assert uploaded['digest'] == archive_digest
    return client.request('handler-releases/stage', dict(manifest=manifest, release_digest=digest,
        archive_digest=archive_digest, archive_bytes=archive.stat().st_size))


def activate(client, handler, digest, generation, request_id):
    return client.request('handler-releases/activate', dict(handler_id=handler, release_digest=digest,
        expected_generation=generation, request_id=request_id))


def test_admin_stages_activates_idempotently_and_refuses_stale_generation(authority, tmp_path):
    _server, clients = authority
    first = package(tmp_path, 'a')
    staged = stage(clients['operator'], first)
    assert staged['release_digest'] == first[1]
    receipt = activate(clients['operator'], 'test.v1', first[1], 0, '1'*32)
    assert receipt['generation'] == 1
    assert activate(clients['operator'], 'test.v1', first[1], 0, '1'*32)['idempotent'] is True
    with pytest.raises(WorkloadError, match='handler_registry_generation_conflict'):
        activate(clients['operator'], 'test.v1', first[1], 0, '2'*32)
    status = clients['operator'].request('handler-releases/status')
    assert status['defaults'] == {'test.v1': first[1]}
    assert status['recent_receipts'][0]['generation'] == 1


# The per-handler release-count refusal was removed (storage is the bound, with burst headroom):
# see test_workload_handler_registry_burst.py.


def test_caller_cannot_register_and_archive_must_match_manifest(authority, tmp_path):
    _server, clients = authority
    first = package(tmp_path, 'a')
    manifest, digest, archive, archive_digest = first
    uploaded = InputTransfer(clients['operator']).put(archive)
    request = dict(manifest=manifest, release_digest=digest, archive_digest=archive_digest,
                   archive_bytes=archive.stat().st_size)
    with pytest.raises(WorkloadError, match='handler release staging requires an admin'):
        clients['caller'].request('handler-releases/stage', request)
    with pytest.raises(WorkloadError, match='handler release staging requires an admin'):
        clients['worker'].request('handler-releases/stage', request)
    archive.write_bytes(archive.read_bytes()+b'tamper')
    uploaded = InputTransfer(clients['operator']).put(archive)
    request.update(archive_digest=uploaded['digest'], archive_bytes=uploaded['size'])
    with pytest.raises(WorkloadError, match='handler_archive_noncanonical_size'):
        clients['operator'].request('handler-releases/stage', request)


def test_package_cannot_expand_runtime_backend_or_compilation_privileges(authority, tmp_path):
    _server, clients = authority
    manifest, _digest, archive, archive_digest = package(tmp_path, 'policy')
    manifest['backend'] = 'rootless-docker'
    widened = validate_manifest(manifest)
    request = dict(manifest=manifest, release_digest=widened['release_digest'],
        archive_digest=archive_digest, archive_bytes=archive.stat().st_size)
    InputTransfer(clients['operator']).put(archive)
    with pytest.raises(WorkloadError, match='handler_release_policy_refused'):
        clients['operator'].request('handler-releases/stage', request)

    manifest['backend'] = 'native'
    manifest['runtime_id'] = 'uninstalled-runtime'
    unsupported_runtime = validate_manifest(manifest)
    request.update(manifest=manifest, release_digest=unsupported_runtime['release_digest'])
    with pytest.raises(WorkloadError, match='handler_release_policy_refused'):
        clients['operator'].request('handler-releases/stage', request)

    # Signing and compilation are authority/worker policy. Unknown manifest
    # fields cannot turn package metadata into permission grants.
    manifest['signing'] = True
    manifest['compilation'] = True
    with pytest.raises(WorkloadError, match='handler_manifest_unknown_or_missing_fields'):
        clients['operator'].request('handler-releases/stage', request)


def test_archive_path_escape_is_refused_before_catalog_registration(authority, tmp_path):
    _server, clients = authority
    manifest, digest, archive_path, _archive_digest = package(tmp_path, 'escape')
    source = (FIXTURE/manifest['files'][0]['path']).read_bytes()
    with tarfile.open(archive_path, 'w', format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo('../escape.py')
        member.mode, member.size, member.mtime, member.uid, member.gid = 0o444, len(source), 0, 0, 0
        member.uname = member.gname = ''
        archive.addfile(member, io.BytesIO(source))
    bundle = (manifest, digest, archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest())
    with pytest.raises(WorkloadError, match='handler_archive_member_refused'):
        stage(clients['operator'], bundle)


def test_generation_compare_and_swap_allows_only_one_concurrent_activation(authority, tmp_path):
    _server, clients = authority
    releases = [package(tmp_path, version) for version in ('race-a', 'race-b')]
    for release in releases:
        stage(clients['operator'], release)

    def activate_one(index):
        try:
            receipt = activate(clients['operator'], 'test.v1', releases[index][1], 0,
                               f'{index+10:032x}')
            return ('activated', receipt)
        except WorkloadError as error:
            return ('refused', str(error))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(activate_one, (0, 1)))
    assert sum(outcome[0] == 'activated' for outcome in outcomes) == 1
    refused = next(value for kind, value in outcomes if kind == 'refused')
    assert 'handler_registry_generation_conflict' in refused
    status = clients['operator'].request('handler-releases/status')
    assert status['generation'] == 1
    assert status['defaults']['test.v1'] in {release[1] for release in releases}


def test_worker_install_pointer_and_restart_verify_complete_package(tmp_path, monkeypatch):
    manifest, digest, archive, archive_digest = package(tmp_path, 'worker-a')
    next_manifest, next_digest, next_archive, next_archive_digest = package(tmp_path, 'worker-b')
    descriptor = dict(manifest=manifest, release_digest=digest, archive_digest=archive_digest,
                      archive_bytes=archive.stat().st_size)
    store = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'test.v1': {'backend': 'native', 'outputs': ['stale'], 'max_seconds': 60}},
        platform_name=manifest['platform'], architecture=manifest['architecture'])
    installed = store.install(archive, descriptor)
    assert store.verify_installed(digest) == manifest
    config = store.handler_config(digest)
    assert config['outputs'] == manifest['outputs']
    assert config['max_seconds'] == 60  # package metadata cannot expand worker resource policy
    assert Path(config['argv'][1]).is_relative_to(installed)
    store.commit_pointer(4, {'test.v1': digest})
    store.install(next_archive, dict(manifest=next_manifest, release_digest=next_digest,
        archive_digest=next_archive_digest, archive_bytes=next_archive.stat().st_size))

    # Simulate a worker crash before the atomic pointer rename. The new package
    # is harmless on disk and restart still selects the previous complete set.
    from livestack_node.workloads import handler_installer
    original_replace = handler_installer.os.replace
    def fail_pointer_replace(source, destination):
        if Path(destination) == store.pointer:
            raise OSError('simulated crash before registry pointer commit')
        return original_replace(source, destination)
    with monkeypatch.context() as patcher:
        patcher.setattr(handler_installer.os, 'replace', fail_pointer_replace)
        with pytest.raises(OSError, match='simulated crash'):
            store.commit_pointer(5, {'test.v1': next_digest})

    reloaded = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'test.v1': {'backend': 'native'}}, platform_name=manifest['platform'],
        architecture=manifest['architecture'])
    assert reloaded.read_pointer() == {'generation': 4, 'defaults': {'test.v1': digest}}
    assert digest in {item['release_digest'] for item in reloaded.inventory(4)['releases']}
    store.commit_pointer(5, {'test.v1': next_digest})
    after_commit = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'test.v1': {'backend': 'native'}}, platform_name=manifest['platform'],
        architecture=manifest['architecture'])
    assert after_commit.read_pointer() == {'generation': 5, 'defaults': {'test.v1': next_digest}}


def test_authority_activation_crash_before_commit_keeps_previous_generation(authority, tmp_path):
    server, clients = authority
    release = package(tmp_path, 'crash')
    stage(clients['operator'], release)
    registry = server.handler_registry
    original_event = registry._event

    def crash_before_commit(db, actor, handler, digest, archive_digest, size, outcome):
        if outcome == 'activated':
            raise RuntimeError('simulated authority crash before transaction commit')
        return original_event(db, actor, handler, digest, archive_digest, size, outcome)

    registry._event = crash_before_commit
    try:
        with pytest.raises(RuntimeError, match='simulated authority crash'):
            registry.activate('operator', dict(request_id='9'*32, expected_generation=0,
                handler_id='test.v1', release_digest=release[1]))
    finally:
        registry._event = original_event

    before = clients['operator'].request('handler-releases/status')
    assert before['generation'] == 0
    assert before['defaults'] == {}
    assert before['recent_receipts'] == []
    receipt = activate(clients['operator'], 'test.v1', release[1], 0, '9'*32)
    assert receipt['generation'] == 1


def test_collection_preserves_current_and_rollback_releases_then_deletes_only_aged_unreferenced(authority, tmp_path):
    server, clients = authority
    releases = [package(tmp_path, version) for version in ('a', 'b', 'c')]
    generation = 0
    for index, release in enumerate(releases):
        stage(clients['operator'], release)
        activate(clients['operator'], 'test.v1', release[1], generation, f'{index+5:032x}')
        generation += 1
    with server.store.transaction() as db:
        db.execute('UPDATE handler_releases SET created=0')
    receipt = server.handler_registry.collect(actor='operator')
    assert receipt['outcome'] == 'complete', receipt
    assert receipt['deleted'] == 1
    status = clients['operator'].request('handler-releases/status')
    installed = {row['release_digest'] for row in status['releases']}
    assert installed == {releases[1][1], releases[2][1]}
    # The current C selection and previous B selection remain available for rollback.


def test_authority_collection_refuses_when_reference_evidence_is_unavailable(authority, tmp_path, monkeypatch):
    server, clients = authority
    release = package(tmp_path, 'missing-references')
    stage(clients['operator'], release)
    with server.store.transaction() as db:
        db.execute('UPDATE handler_releases SET created=0')

    def evidence_unavailable():
        raise OSError('simulated unavailable reference database')
    monkeypatch.setattr(server.store, 'transaction', evidence_unavailable)
    receipt = server.handler_registry.collect(actor='operator')
    assert receipt['outcome'] == 'refused'
    assert receipt['reason'] == 'handler_release_reference_evidence_unavailable'
    monkeypatch.undo()
    status = clients['operator'].request('handler-releases/status')
    assert any(row['release_digest'] == release[1] for row in status['releases'])


def test_unconfigured_worker_retention_refuses_local_deletion(tmp_path):
    manifest, digest, archive, archive_digest = package(tmp_path, 'worker')
    descriptor = dict(manifest=manifest, release_digest=digest, archive_digest=archive_digest,
                      archive_bytes=archive.stat().st_size)
    store = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'test.v1': {'backend': 'native'}}, platform_name=manifest['platform'],
        architecture=manifest['architecture'])
    store.install(archive, descriptor)
    result = store.prune(set(), set(), None, generation=0, references_complete=True)
    assert result['reason'] == 'handler_release_retention_unconfigured'
    missing = store.prune(set(), set(), 24*60*60, generation=0, references_complete=False)
    assert missing['reason'] == 'handler_release_reference_evidence_unavailable'
    assert (tmp_path/'worker-state'/digest).is_dir()


def test_worker_store_recovers_bounded_interrupted_package_staging(tmp_path):
    root = tmp_path/'worker-state'
    root.mkdir(mode=0o700)
    staging = root/'.handler-stage-interrupted'
    staging.mkdir()
    (staging/'partial').write_bytes(b'incomplete package')
    (root/('.handler-download-'+'a'*64+'.tar')).write_bytes(b'incomplete archive')
    (root/'.registry-current.tmp').write_text('{"generation":999}')
    HandlerPackageStore(root, {'node22': sys.executable}, {'test.v1': {'backend': 'native'}})
    assert list(root.iterdir()) == []


def test_worker_package_root_refuses_excess_entries(tmp_path):
    root = tmp_path/'worker-state'
    root.mkdir(mode=0o700)
    for index in range(261):
        (root/f'unknown-{index}').touch()
    with pytest.raises(WorkloadError, match='handler_registry_directory_capacity'):
        HandlerPackageStore(root, {'node22': sys.executable}, {'test.v1': {'backend': 'native'}})


def test_documented_release_cli_stages_activates_and_reports(authority, tmp_path, capsys):
    server, _clients = authority
    handler_manifest, digest, archive, archive_digest = package(tmp_path, 'cli')
    bundle = tmp_path/'bundle'
    package_dir = bundle/'handler-packages'
    package_dir.mkdir(parents=True)
    archive_rel = f'handler-packages/{digest}.tar'
    bundled_archive = bundle/archive_rel
    bundled_archive.write_bytes(archive.read_bytes())
    descriptor = dict(release_digest=digest, archive_digest=archive_digest, archive_path=archive_rel)
    bundled_archive.with_suffix('.json').write_text(json.dumps(dict(
        manifest=handler_manifest, release_digest=digest, archive_digest=archive_digest)))
    (bundle/'manifest.json').write_text(json.dumps({'handler_releases': {'test.v1': descriptor}}))
    operator_config = tmp_path/'operator.json'
    operator_config.write_text(json.dumps(dict(authority=f'http://127.0.0.1:{server.server_port}',
                                                token='o'*32)))

    assert release_cli_main(['--config', str(operator_config), 'stage', '--bundle', str(bundle),
                             '--handler', 'test.v1']) == 0
    assert 'staged' in capsys.readouterr().out
    assert release_cli_main(['--config', str(operator_config), 'activate', '--handler', 'test.v1',
                             '--digest', digest, '--expected-generation', '0',
                             '--request-id', 'd'*32]) == 0
    assert '"generation": 1' in capsys.readouterr().out
    assert release_cli_main(['--config', str(operator_config), 'status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['defaults'] == {'test.v1': digest}


def test_default_submission_keeps_original_digest_and_placement_is_exact(authority, tmp_path):
    _server, clients = authority
    first, second = package(tmp_path, 'a'), package(tmp_path, 'b')
    stage(clients['operator'], first)
    activate(clients['operator'], 'test.v1', first[1], 0, '3'*32)
    source = b'input'
    input_digest = hashlib.sha256(source).hexdigest()
    # Caller uploads input using the same authenticated immutable object API.
    InputTransfer(clients['caller']).put(_write(tmp_path/'input', source))
    request = dict(version=1, key='same-intent', handler='test.v1', input_digest=input_digest,
                   need={'cpu': 1, 'memory_bytes': 128*1024**2},
                   handler_release={'selection': 'default'})
    first_job = clients['caller'].submit(request)
    stage(clients['operator'], second)
    activate(clients['operator'], 'test.v1', second[1], 1, '4'*32)
    repeated = clients['caller'].submit(request)
    assert repeated['id'] == first_job['id']
    assert repeated['spec']['handler_release']['release_digest'] == first[1]
    with pytest.raises(WorkloadError, match='idempotency key already names different inputs'):
        clients['caller'].submit(dict(request, handler_release={'selection': 'exact', 'release_digest': second[1]}))

    identity = dict(handler_id='test.v1', release_digest=first[1], execution_contract=1,
        payload_schema=first[0]['payload_schema'], result_schema=first[0]['result_schema'])
    identity_b = dict(identity, release_digest=second[1])
    report_b = dict(capacity={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3},
        available={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3}, labels={},
        handlers=['test.v1'], ready=True, handler_inventory={'generation': 2,
            'defaults': {'test.v1': second[1]}, 'releases': [identity_b]})
    clients['worker-b'].request('worker/report', dict(boot='boot-b', report=report_b))
    assert clients['worker-b'].request('worker/claim', {'boot': 'boot-b'})['assignment'] is None
    waiting = clients['caller'].get(first_job['id'])
    assert 'no fresh worker advertises release' in waiting['reason']
    report = dict(capacity={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3},
        available={'cpu': 2, 'memory_bytes': 2*1024**3, 'disk_bytes': 20*1024**3}, labels={},
        handlers=['test.v1'], ready=True, handler_inventory={'generation': 1,
            'defaults': {'test.v1': first[1]}, 'releases': [identity]})
    clients['worker'].request('worker/report', dict(boot='boot-1', report=report))
    assignment = clients['worker'].request('worker/claim', {'boot': 'boot-1'})['assignment']
    assert assignment['handler_release']['release_digest'] == first[1]
    wrong = dict(boot='boot-1', attempt_id=assignment['attempt_id'], fence=assignment['fence'],
        input_digest=input_digest, outcome='succeeded', result={'artifacts': []},
        handler_result_identity=dict(job_id=assignment['job_id'], attempt_id=assignment['attempt_id'],
            fence=assignment['fence'], worker=assignment['worker'], boot=assignment['boot'],
            input_digest=input_digest, handler_release={**identity, 'release_digest': second[1]}))
    with pytest.raises(WorkloadError, match='handler_result_release_identity_mismatch'):
        clients['worker'].request('worker/complete', wrong)


def test_infrastructure_retry_keeps_accepted_release_after_default_changes(authority, tmp_path):
    server, clients = authority
    first, second = package(tmp_path, 'retry-a'), package(tmp_path, 'retry-b')
    stage(clients['operator'], first)
    activate(clients['operator'], 'test.v1', first[1], 0, '5'*32)
    source = b'retry-input'
    input_digest = hashlib.sha256(source).hexdigest()
    InputTransfer(clients['caller']).put(_write(tmp_path/'retry-input', source))
    job = clients['caller'].submit(dict(version=1, key='retry-release-a', handler='test.v1',
        input_digest=input_digest, need={'cpu': 1, 'memory_bytes': 128*1024**2},
        handler_release={'selection': 'default'}))

    def identity(release):
        manifest = release[0]
        return dict(handler_id='test.v1', release_digest=release[1], execution_contract=1,
            payload_schema=manifest['payload_schema'], result_schema=manifest['result_schema'])

    capacity = dict(cpu=2, memory_bytes=2*1024**3, disk_bytes=20*1024**3)
    def report(releases, generation, default):
        identities = sorted((identity(item) for item in releases),
                            key=lambda item: (item['handler_id'], item['release_digest']))
        return dict(boot='boot-retry', report=dict(capacity=capacity, available=capacity, labels={},
            handlers=['test.v1'], ready=True, handler_inventory=dict(generation=generation,
                defaults={'test.v1': default}, releases=identities)))

    clients['worker'].request('worker/report', report([first], 1, first[1]))
    assignment = clients['worker'].request('worker/claim', {'boot': 'boot-retry'})['assignment']
    assert assignment['handler_release']['release_digest'] == first[1]
    result_identity = dict(job_id=assignment['job_id'], attempt_id=assignment['attempt_id'],
        fence=assignment['fence'], worker=assignment['worker'], boot=assignment['boot'],
        input_digest=input_digest, handler_release=identity(first))
    retry = clients['worker'].request('worker/complete', dict(boot='boot-retry',
        attempt_id=assignment['attempt_id'], fence=assignment['fence'], input_digest=input_digest,
        outcome='infrastructure', result={'exit_code': 75, 'artifacts': []},
        handler_result_identity=result_identity))
    assert retry['state'] == 'queued'

    stage(clients['operator'], second)
    activate(clients['operator'], 'test.v1', second[1], 1, '6'*32)
    clients['worker'].request('worker/report', report([first, second], 2, second[1]))
    retried = clients['worker'].request('worker/claim', {'boot': 'boot-retry'})['assignment']
    assert retried['handler_release']['release_digest'] == first[1]
    assert retried['spec']['handler_release']['release_digest'] == first[1]
    terminal_identity = dict(job_id=retried['job_id'], attempt_id=retried['attempt_id'],
        fence=retried['fence'], worker=retried['worker'], boot=retried['boot'],
        input_digest=input_digest, handler_release=identity(first))
    result = clients['worker'].request('worker/complete', dict(boot='boot-retry',
        attempt_id=retried['attempt_id'], fence=retried['fence'], input_digest=input_digest,
        outcome='product_failure', result={'exit_code': 2, 'artifacts': []},
        handler_result_identity=terminal_identity))
    assert result['state'] == 'failed'
    assert result['spec']['handler_release']['release_digest'] == first[1]
    assert result['result']['handler_result_identity']['handler_release']['release_digest'] == first[1]
    with server.store.transaction() as db:
        count = db.execute('SELECT count(*) FROM attempts WHERE job=?', (job['id'],)).fetchone()[0]
    assert count == 2


def test_deadline_expiry_keeps_original_accepted_release(authority, tmp_path, monkeypatch):
    server, clients = authority
    first, second = package(tmp_path, 'deadline-a'), package(tmp_path, 'deadline-b')
    stage(clients['operator'], first)
    activate(clients['operator'], 'test.v1', first[1], 0, '8'*32)
    clock = [server.store.clock()]
    monkeypatch.setattr(server.store, 'clock', lambda: clock[0])
    source = b'deadline-input'
    input_digest = hashlib.sha256(source).hexdigest()
    InputTransfer(clients['caller']).put(_write(tmp_path/'deadline-input', source))
    job = clients['caller'].submit(dict(version=1, key='deadline-release-a', handler='test.v1',
        input_digest=input_digest, deadline=clock[0]+10,
        need={'cpu': 1, 'memory_bytes': 128*1024**2}, handler_release={'selection': 'default'}))

    stage(clients['operator'], second)
    activate(clients['operator'], 'test.v1', second[1], 1, 'a'*32)
    clock[0] += 20
    expired = clients['caller'].get(job['id'])
    assert expired['state'] == 'expired'
    assert expired['spec']['handler_release']['release_digest'] == first[1]


def test_handler_reference_reconciliation_database_work_is_constant(authority, tmp_path, monkeypatch):
    server, clients = authority
    release = package(tmp_path, 'fanout')
    stage(clients['operator'], release)
    activate(clients['operator'], 'test.v1', release[1], 0, '7'*32)
    source = b'fanout-input'
    input_digest = hashlib.sha256(source).hexdigest()
    InputTransfer(clients['caller']).put(_write(tmp_path/'fanout-input', source))

    def submit(count, offset):
        for index in range(count):
            clients['caller'].submit(dict(version=1, key=f'fanout-{offset+index}', handler='test.v1',
                input_digest=input_digest, need={'cpu': 1, 'memory_bytes': 128*1024**2},
                handler_release={'selection': 'default'}))

    original_connect = server.store.connect
    statements = []
    checkouts = [0]
    def tracked_connect():
        checkouts[0] += 1
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(server.store, 'connect', tracked_connect)

    def collect_work():
        statements.clear()
        checkouts[0] = 0
        receipt = server.handler_registry.collect(actor='operator')
        assert receipt['outcome'] == 'complete', receipt
        selects = [sql for sql in statements if 'WITH referenced(digest) AS MATERIALIZED' in sql]
        assert len(selects) == 1
        return checkouts[0], len(statements)

    submit(1, 0)
    one_job_work = collect_work()
    submit(32, 1)
    many_jobs_work = collect_work()
    assert one_job_work == many_jobs_work


def _write(path, content):
    path.write_bytes(content)
    return path
