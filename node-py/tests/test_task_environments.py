import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from livestack_node.workloads.archive import capture, unpack
from livestack_node.fleet_operations import CREATED, OperationStore
from livestack_node.fleet_ops_api import deprovision
from livestack_node.hostd import _drain_blocked
from livestack_node.hostbroker import HostBroker
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads import task_environments as task_environments_module
from livestack_node.workloads.task_environments import TaskEnvironmentStore


GIB = 1024**3
OWNER = hashlib.sha256(b'alice\0').hexdigest()
PROFILE = 'benchday-linux-dev-v1'
HANDLER = 'benchday.compilation.flutter-check.v1'


def profile(*, probe='print("flutter 3.24.0")', contract='cache-v1'):
    return {PROFILE: dict(handlers=[HANDLER], purpose='development', cache_contract=contract,
        probe_argv=[sys.executable, '-c', probe], cache_components=[
            dict(name='dart-dependencies', path='source/.dart_tool', inputs=['pubspec.lock'],
                 contract='pub-v1'),
            dict(name='flutter-build', path='source/build', inputs=['pubspec.lock'],
                 contract='flutter-debug-v1')])}


def make_store(tmp_path, *, now=None, max_per_owner=128*GIB, max_total=256*GIB, profile_spec=None,
               max_per_replica=32*GIB, idle_seconds=10, generation_seconds=100):
    root = tmp_path/'environments'
    workspace = tmp_path/'attempt-workspace'
    root.mkdir()
    workspace.mkdir()
    quota_calls = []
    def ensure(handle, project_id, quota_bytes):
        quota_calls.append((handle, project_id, quota_bytes))
        return dict(quota_bytes=quota_bytes, used_bytes=0)
    def usage(project_ids):
        rows = []
        for directory in root.iterdir():
            marker_path = directory/'environment.json'
            if not marker_path.is_file():
                continue
            marker = json.loads(marker_path.read_text())
            if marker['project_id'] not in project_ids:
                continue
            used = sum(path.lstat().st_blocks*512 for path in directory.rglob('*') if not path.is_dir())
            rows.append(dict(project_id=marker['project_id'], used_bytes=used,
                hard_bytes=((marker['quota_bytes']+1023)//1024)*1024))
        return rows
    config = dict(root=str(root), host_id='host-a', quota_helper='/usr/local/libexec/quota',
        project_id_min=100000, project_id_max=1000000, max_bytes_per_replica=max_per_replica,
        max_bytes_per_owner=max_per_owner, max_total_bytes=max_total, reserve_bytes=0,
        idle_seconds=idle_seconds, generation_seconds=generation_seconds,
        profiles=profile_spec or profile())
    store = TaskEnvironmentStore(config, workspace=workspace, handlers=[HANDLER],
        quota_ensure=ensure, quota_probe=lambda _: True, quota_usage=usage,
        require_separate_filesystem=False,
        filesystem_bytes=512*GIB,
        clock=(lambda: now[0]) if now is not None else time.time)
    return store, root, quota_calls


def bundle(tmp_path, files):
    source = tmp_path/'repo'
    source.mkdir(exist_ok=True)
    for name, content in files.items():
        path = source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    # Capture only this manifest's complete source closure. Leave old files in
    # place so the environment mirror must remove files absent from a revision.
    archive = tmp_path/f'capture-{hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()[:8]}.tar'
    captured = capture(source, list(files), archive)
    attempt = tmp_path/f'attempt-{captured["digest"][:8]}-{time.time_ns()}'
    unpack(archive, attempt, captured['digest'])
    return attempt, captured['digest']


def assignment(handle, generation, digest, *, replicas=(), owner_scope=OWNER):
    return dict(job_id=f'job-{generation}', spec=dict(handler=HANDLER, input_digest=digest),
        environment=dict(handle=handle, purpose='development', profile=PROFILE, generation=generation,
            compatibility=None, owner_scope=owner_scope, queue_seconds=2.5, replicas=list(replicas)))


def phase_timings():
    return {phase: {'seconds': 0.1} for phase in
            ('queue', 'transfer', 'source_materialization', 'dependencies', 'compile', 'test',
             'execution', 'cleanup')}


def finish(store, prepared, *, generation, bytes_used=None):
    receipt = store.receipt(prepared, state='parked', phase_timings=phase_timings())
    store.verify_source(prepared)
    store.mark_awaiting_authority(prepared, state='parked', bytes_used=receipt['bytes_used'])
    assert store.acknowledge(prepared, {'state': 'parked', 'handle': prepared['handle'],
                                        'generation': generation})
    store.release(prepared)
    return receipt


def local_replica(store, handle, generation):
    profiles, replicas = store.report()
    assert profiles[PROFILE]
    replica = next(item for item in replicas if item['handle'] == handle)
    return dict(host='host-a', profile=replica['profile'], compatibility=replica['compatibility'],
        generation=generation, state=replica['state'])


def test_complete_mirror_preserves_incremental_state_and_invalidates_lockfiles(tmp_path):
    store, _, quota_calls = make_store(tmp_path)
    handle = 'a'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock-a', 'lib/main.dart': b'void main() {}',
                                         'lib/deleted.dart': b'old'})
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    (first['source']/'.dart_tool'/'package_config.json').write_text('resolved deps')
    (first['source']/'build'/'kernel.dill').write_text('compiled')
    first_receipt = finish(store, first, generation=1)
    assert first_receipt['reuse_outcome'] == 'created'
    assert first_receipt['reason_code'] == 'created'
    assert quota_calls[0][2] == 32*GIB
    replica = local_replica(store, handle, 1)
    assert replica['generation'] == 1

    lock_mtime = (store.root/handle/'source'/'pubspec.lock').stat().st_mtime_ns
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock-a', 'lib/main.dart': b'void main() { print(1); }',
                                         'lib/new.dart': b'new'})
    second = store.prepare(assignment(handle, 2, digest, replicas=[replica]), incoming, handler=HANDLER)
    assert second['reuse_outcome'] == 'reused'
    assert second['reason_code'] == 'source_updated_incrementally'
    assert {item['name']: item['outcome'] for item in second['cache_components']} == {
        'dart-dependencies': 'reused', 'flutter-build': 'reused'}
    assert (second['source']/'.dart_tool'/'package_config.json').read_text() == 'resolved deps'
    assert (second['source']/'build'/'kernel.dill').read_text() == 'compiled'
    assert not (second['source']/'lib'/'deleted.dart').exists()
    assert not (second['source']/'untracked.tmp').exists()
    assert (second['source']/'pubspec.lock').stat().st_mtime_ns == lock_mtime
    finish(store, second, generation=2)

    replica = local_replica(store, handle, 2)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock-b', 'lib/main.dart': b'void main() { print(1); }'})
    third = store.prepare(assignment(handle, 3, digest, replicas=[replica]), incoming, handler=HANDLER)
    assert third['reuse_outcome'] == 'reused'
    assert third['reason_code'] == 'cache_inputs_changed'
    assert {item['name']: item['outcome'] for item in third['cache_components']} == {
        'dart-dependencies': 'invalidated', 'flutter-build': 'invalidated'}
    assert not (third['source']/'.dart_tool'/'package_config.json').exists()
    assert not (third['source']/'build'/'kernel.dill').exists()
    finish(store, third, generation=3)


def test_incompatible_profile_rebuilds_and_remote_replica_relocates(tmp_path):
    store, root, _ = make_store(tmp_path)
    handle = 'b'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    (first['source']/'.dart_tool'/'cache').write_text('old')
    finish(store, first, generation=1)
    old_replica = local_replica(store, handle, 1)

    changed_profile = profile(probe='print("flutter 3.25.0")')
    changed = TaskEnvironmentStore(dict(root=str(root), host_id='host-a',
        quota_helper='/usr/local/libexec/quota', project_id_min=100000, project_id_max=1000000,
        max_bytes_per_replica=32*GIB, max_bytes_per_owner=128*GIB, max_total_bytes=256*GIB,
        reserve_bytes=0, idle_seconds=10, generation_seconds=100, profiles=changed_profile),
        workspace=tmp_path/'attempt-workspace', handlers=[HANDLER],
        quota_ensure=lambda handle, project, quota: {'quota_bytes': quota},
        quota_probe=lambda _: True, require_separate_filesystem=False, filesystem_bytes=512*GIB)
    rebuilt = changed.prepare(assignment(handle, 2, digest, replicas=[old_replica]), incoming, handler=HANDLER)
    assert rebuilt['reuse_outcome'] == 'rebuilt'
    assert rebuilt['reason_code'] == 'toolchain_changed'
    assert rebuilt['compatibility'] != old_replica['compatibility']
    finish(changed, rebuilt, generation=2)

    remote = assignment('c'*32, 1, digest, replicas=[dict(host='host-b', profile=PROFILE,
        compatibility=changed.profile_digest(PROFILE), generation=1, state='parked')])
    relocated = changed.prepare(remote, incoming, handler=HANDLER)
    assert relocated['reuse_outcome'] == 'relocated'
    assert relocated['reason_code'] == 'relocated_reconstructed'
    changed.release(relocated)


def test_profile_probe_refresh_invalidates_cache_after_toolchain_replacement(tmp_path):
    version = tmp_path/'toolchain-version'
    version.write_text('compiler-v1')
    probe = f'from pathlib import Path; print(Path({str(version)!r}).read_text())'
    store, _, _ = make_store(tmp_path, profile_spec=profile(probe=probe))
    handle = '5'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    (first['source']/'.dart_tool'/'cache').write_text('compiled with v1')
    old_compatibility = first['compatibility']
    finish(store, first, generation=1)

    version.write_text('compiler-v2')
    old_replica = local_replica(store, handle, 1)
    second = store.prepare(assignment(handle, 2, digest, replicas=[old_replica]), incoming,
                           handler=HANDLER)
    assert second['reuse_outcome'] == 'rebuilt'
    assert second['reason_code'] == 'toolchain_changed'
    assert second['compatibility'] != old_compatibility
    assert second['cache_components'][0]['outcome'] == 'invalidated'
    store.release(second)


def test_incomplete_writer_is_never_reported_as_parked_and_generation_fences(tmp_path):
    store, _, _ = make_store(tmp_path)
    handle = 'd'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    prepared = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    assert store.report()[1] == []
    store.mark_awaiting_authority(prepared, state='parked', bytes_used=10)
    # A completion receipt without authority acknowledgement is still private
    # and cannot become a scheduler cache hit after a worker restart.
    assert store.report()[1] == []
    store.release(prepared)
    store.invalidate(handle)
    assert store.report()[1] == []

    prepared = store.prepare(assignment(handle, 2, digest), incoming, handler=HANDLER)
    store.release(prepared)
    with pytest.raises(WorkloadError, match='not newer'):
        store.prepare(assignment(handle, 2, digest), incoming, handler=HANDLER)


def test_inventory_accepts_only_private_ext4_lost_found(tmp_path, monkeypatch):
    store, root, _ = make_store(tmp_path)
    lost_found = root/'lost+found'
    lost_found.mkdir(mode=0o700)
    real_lstat = Path.lstat
    reported = {'uid': 0, 'mode': 0o700}

    def lstat(path):
        info = real_lstat(path)
        if path == lost_found:
            return SimpleNamespace(st_mode=stat.S_IFDIR | reported['mode'],
                st_uid=reported['uid'], st_gid=0, st_dev=info.st_dev)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    assert store._inventory() == []

    reported['uid'] = os.geteuid() + 1
    with pytest.raises(WorkloadError, match='invalid filesystem recovery directory'):
        store._inventory()

    reported.update(uid=0, mode=0o755)
    with pytest.raises(WorkloadError, match='invalid filesystem recovery directory'):
        store._inventory()


def test_owner_and_host_hard_quota_reservations_are_bounded(tmp_path):
    store, _, calls = make_store(tmp_path, max_per_owner=32*GIB, max_total=64*GIB)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment('e'*32, 1, digest), incoming, handler=HANDLER)
    store.release(first)
    with pytest.raises(WorkloadError, match='budget is exhausted'):
        store.prepare(assignment('f'*32, 1, digest), incoming, handler=HANDLER)
    assert len(calls) == 1
    other_owner = store.prepare(assignment('1'*32, 1, digest, owner_scope='2'*64), incoming, handler=HANDLER)
    assert other_owner['metadata']['quota_bytes'] == 32*GIB
    store.release(other_owner)


def test_concurrent_environment_creations_cannot_overreserve_host_quota(tmp_path):
    store, _, calls = make_store(tmp_path, max_total=32*GIB)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    barrier = threading.Barrier(2)
    outcomes = []

    def prepare(handle):
        barrier.wait(timeout=2)
        try:
            prepared = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
        except WorkloadError as error:
            outcomes.append(str(error))
            return
        outcomes.append('admitted')
        store.release(prepared)

    threads = [threading.Thread(target=prepare, args=(handle,)) for handle in ('8'*32, '9'*32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count('admitted') == 1
    assert any('budget is exhausted' in outcome for outcome in outcomes)
    assert len(calls) == 1


def test_prune_expires_parked_disk_only_and_reports_deletion(tmp_path):
    now = [1000.0]
    store, root, _ = make_store(tmp_path, now=now)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    prepared = store.prepare(assignment('2'*32, 1, digest), incoming, handler=HANDLER)
    finish(store, prepared, generation=1)
    now[0] += 11
    result = store.prune()
    assert result['removed'] == ['2'*32]
    assert not (root/('2'*32)).exists()
    recreated = store.prepare(assignment('2'*32, 2, digest), incoming, handler=HANDLER)
    assert recreated['reuse_outcome'] == 'rebuilt'
    assert (recreated['source']/'lib'/'main.dart').read_bytes() == b'code'
    store.release(recreated)


def test_prune_removes_only_one_bounded_batch_then_revisits_remaining_entries(tmp_path):
    now = [1000.0]
    store, root, _ = make_store(tmp_path, now=now, max_per_replica=GIB, max_total=128*GIB)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    handles = [f'{index:032x}' for index in range(64)]
    for index, handle in enumerate(handles):
        prepared = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
        finish(store, prepared, generation=1)
    now[0] += 11

    with pytest.raises(ValueError, match='may only be lowered'):
        store.prune(rows=65)
    first = store.prune(rows=32)
    assert first['examined'] == 32
    assert len(first['removed']) == 32
    remaining = [handle for handle in handles if (root/handle).exists()]
    assert len(remaining) == 32

    second = store.prune(rows=32)
    assert second['examined'] == 32
    assert set(second['removed']) == set(remaining)
    assert not any((root/handle).exists() for handle in handles)


def test_prune_protects_expired_writer_until_supervised_cleanup(tmp_path):
    now = [1000.0]
    store, root, _ = make_store(tmp_path, now=now)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    handle = 'a'*32
    prepared = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    now[0] += 101

    assert store.prune()['removed'] == []
    assert (root/handle).exists(), 'the active writer still owns its lock and quota'

    finish(store, prepared, generation=1)
    assert store.prune()['removed'] == [handle]
    assert not (root/handle).exists()


def test_absolute_generation_expiry_survives_recent_idle_refresh(tmp_path):
    now = [1000.0]
    store, root, _ = make_store(tmp_path, now=now, idle_seconds=200, generation_seconds=100)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    handle = 'b'*32
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    finish(store, first, generation=1)

    now[0] += 95
    replica = local_replica(store, handle, 1)
    second = store.prepare(assignment(handle, 2, digest, replicas=[replica]), incoming, handler=HANDLER)
    finish(store, second, generation=2)
    marker = json.loads((root/handle/'environment.json').read_text())
    assert marker['generation_expires'] == 1100
    assert marker['idle_expires'] > marker['generation_expires']

    now[0] = 1099
    assert store.prune()['removed'] == []
    now[0] = 1100
    assert store.prune()['removed'] == [handle]


def test_deletion_failure_keeps_host_quota_reserved_until_retry(tmp_path, monkeypatch):
    now = [1000.0]
    store, root, quota_calls = make_store(tmp_path, now=now, max_total=32*GIB)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    expired_handle = 'c'*32
    prepared = store.prepare(assignment(expired_handle, 1, digest), incoming, handler=HANDLER)
    finish(store, prepared, generation=1)
    now[0] += 11

    # No integration test can safely inject a transient deletion failure into
    # the kernel-quota mount; this control verifies the host ledger fails closed.
    def refuse_delete(_path):
        raise PermissionError('injected deletion failure')

    monkeypatch.setattr(task_environments_module, '_remove_tree', refuse_delete)
    assert store.prune()['removed'] == []
    assert (root/expired_handle/'environment.json').is_file()
    with pytest.raises(WorkloadError, match='budget is exhausted'):
        store.prepare(assignment('d'*32, 1, digest), incoming, handler=HANDLER)
    assert len(quota_calls) == 1

    monkeypatch.undo()
    assert store.prune()['removed'] == [expired_handle]
    assert not (root/expired_handle).exists()
    recreated = store.prepare(assignment('d'*32, 1, digest), incoming, handler=HANDLER)
    assert recreated['reuse_outcome'] == 'created'
    store.release(recreated)


def test_prune_reclaims_untrusted_environment_after_its_writer_releases(tmp_path):
    store, root, _ = make_store(tmp_path)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    prepared = store.prepare(assignment('7'*32, 1, digest), incoming, handler=HANDLER)
    store.reject(prepared)
    store.release(prepared)
    result = store.prune()
    assert result['removed'] == ['7'*32]
    assert not (root/('7'*32)).exists()


def test_stale_replica_cleanup_yields_to_writer_and_preserves_a_newer_marker(tmp_path):
    # Unit-only: hold the actual flock while an obsolete report races a writer,
    # then change the marker before retry; the worker/HTTP integration covers
    # successful cleanup but cannot deterministically pause at these race points.
    store, root, _ = make_store(tmp_path)
    handle = '9'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    finish(store, first, generation=1)

    current_replica = local_replica(store, handle, 1)
    second = store.prepare(assignment(handle, 2, digest, replicas=[current_replica]), incoming,
                           handler=HANDLER)
    assert store.remove_stale_replica(handle, 1) == 'busy'
    assert json.loads((root/handle/'environment.json').read_text())['generation'] == 2

    finish(store, second, generation=2)
    assert store.remove_stale_replica(handle, 1) == 'changed'
    marker = json.loads((root/handle/'environment.json').read_text())
    assert marker['generation'] == 2 and marker['state'] == 'parked'
    assert store.remove_stale_replica(handle, 2) == 'removed'
    assert not (root/handle).exists()


def test_source_integrity_failure_and_symlinked_cache_force_reconstruction(tmp_path):
    store, _, _ = make_store(tmp_path)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    prepared = store.prepare(assignment('3'*32, 1, digest), incoming, handler=HANDLER)
    (prepared['source']/'lib'/'main.dart').write_text('modified')
    with pytest.raises(WorkloadError, match='modified captured source'):
        store.verify_source(prepared)
    store.reject(prepared)
    store.release(prepared)


def test_internal_npm_style_cache_links_are_reused_but_escape_links_refuse(tmp_path):
    store, _, _ = make_store(tmp_path)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment('4'*32, 1, digest), incoming, handler=HANDLER)
    cache = first['source']/'.dart_tool'
    (cache/'package-config').write_text('resolved')
    (cache/'bin').symlink_to('../lib', target_is_directory=True)
    assert first['cache_components'][0]['path'] == '.dart_tool'
    receipt = finish(store, first, generation=1)
    assert 'path' not in receipt['cache_components'][0]

    replica = local_replica(store, '4'*32, 1)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'changed'})
    second = store.prepare(assignment('4'*32, 2, digest, replicas=[replica]), incoming, handler=HANDLER)
    assert second['cache_components'][0]['outcome'] == 'reused'
    assert (second['source']/'lib'/'main.dart').read_bytes() == b'changed'
    assert (second['source']/'.dart_tool'/'bin').is_symlink()
    (second['source']/'.dart_tool'/'escape').symlink_to('../../../../etc/passwd')
    with pytest.raises(WorkloadError, match='unsafe symlink'):
        store.verify_source(second)
    store.reject(second)
    store.release(second)


def test_captured_source_alias_is_verified_against_benchday_manifest(tmp_path):
    store, _, _ = make_store(tmp_path)
    source_manifest = json.dumps({'version': 1, 'links': [
        {'path': 'packages/core', 'target': 'packages/core-source'}]}).encode()
    incoming, digest = bundle(tmp_path, {'.benchday-source.json': source_manifest,
        'packages/core-source/index.js': b'export {}', 'pubspec.lock': b'lock'})
    prepared = store.prepare(assignment('5'*32, 1, digest), incoming, handler=HANDLER)
    (prepared['source']/'packages/core').symlink_to('core-source', target_is_directory=True)
    store.verify_source(prepared)
    (prepared['source']/'packages/core').unlink()
    (prepared['source']/'packages/core').symlink_to('../../../outside', target_is_directory=True)
    with pytest.raises(WorkloadError, match='manifest'):
        store.verify_source(prepared)
    store.reject(prepared)
    store.release(prepared)

    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    rebuilt = store.prepare(assignment('5'*32, 2, digest), incoming, handler=HANDLER)
    assert rebuilt['cache_components'][0]['outcome'] == 'invalidated'
    external = tmp_path/'external'
    external.mkdir()
    (rebuilt['source']/'.dart_tool'/'bad-link').symlink_to(external)
    with pytest.raises(WorkloadError, match='unsafe symlink'):
        store.verify_source(rebuilt)
    store.reject(rebuilt)
    store.release(rebuilt)


def test_same_handle_local_lock_serializes_distinct_worker_processes(tmp_path):
    store, _, _ = make_store(tmp_path)
    peer = TaskEnvironmentStore(dict(root=str(store.root), host_id='host-a',
        quota_helper='/usr/local/libexec/quota', project_id_min=100000, project_id_max=1000000,
        max_bytes_per_replica=32*GIB, max_bytes_per_owner=128*GIB, max_total_bytes=256*GIB,
        reserve_bytes=0, idle_seconds=10, generation_seconds=100, profiles=profile()),
        workspace=store.workspace, handlers=[HANDLER], quota_ensure=store.quota_ensure,
        quota_probe=store.quota_probe, quota_usage=store.quota_usage,
        require_separate_filesystem=False, filesystem_bytes=512*GIB, clock=store.clock)
    handle = '4'*32
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    first = store.prepare(assignment(handle, 1, digest), incoming, handler=HANDLER)
    started = threading.Event()
    continue_writer = threading.Event()
    finished = threading.Event()
    result = {}

    def second_writer():
        started.set()
        continue_writer.wait(2)
        second = peer.prepare(assignment(handle, 2, digest,
            replicas=[local_replica(store, handle, 1)]), incoming, handler=HANDLER)
        result['prepared'] = second
        finished.set()

    thread = threading.Thread(target=second_writer)
    thread.start()
    assert started.wait(2)
    assert not finished.wait(.1)
    finish(store, first, generation=1)
    continue_writer.set()
    thread.join(3)
    assert finished.is_set()
    assert result['prepared']['reuse_outcome'] == 'reused'
    store.release(result['prepared'])


def test_parked_environment_does_not_block_host_deprovision(tmp_path):
    environment_store, _, _ = make_store(tmp_path)
    incoming, digest = bundle(tmp_path, {'pubspec.lock': b'lock', 'lib/main.dart': b'code'})
    prepared = environment_store.prepare(assignment('6'*32, 1, digest), incoming, handler=HANDLER)
    finish(environment_store, prepared, generation=1)
    profiles, replicas = environment_store.report()
    assert profiles[PROFILE] and len(replicas) == 1
    assert replicas[0]['state'] == 'parked'

    node_id = 'http://worker-a'
    broker = HostBroker(devices=[], peers=[], clock=lambda: 1_700_000_000.0)
    broker.fleet_view = lambda: {'hosts': {'host-a': {'nodes': [
        {'peer': node_id + '/livestack', 'load': {'in_flight': 0}}]}}}
    assert broker.leases_on(node_id) == 0
    operations = OperationStore(str(tmp_path/'fleet.sqlite'), clock=lambda: 1_700_000_000.0)
    operation = operations.claim(job_id='task-environment-drain', owner='owner',
        target_id=node_id, idempotency_key='task-environment-drain', provider='fake',
        tier='SPOT', now=1_700_000_000.0)
    operations.transition(operation.operation_id, CREATED, provider_instance_id='instance-1',
                          now=1_700_000_000.0)
    operations.announce(operation.operation_id, ready=True, node=node_id,
                        now=1_700_000_000.0)
    terminated = []
    result = deprovision({'type': 'deprovision', 'target_id': node_id}, store=operations,
        providers={'fake': SimpleNamespace(terminate=terminated.append)},
        busy=lambda target: _drain_blocked(broker, target), now=1_700_000_000.0)

    assert result['state'] == 'released' and result['teardown'] == 'ok'
    assert terminated == ['instance-1']
    assert environment_store.report()[1][0]['state'] == 'parked'
