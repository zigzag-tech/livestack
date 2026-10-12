"""A registry default built for another platform is a named skip on a worker, not an activation failure."""
import hashlib
import sys

import pytest

from livestack_node.workloads.handler_installer import HandlerPackageStore
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.worker import WorkloadWorker

from test_workload_handler_registry import package


def _worker(tmp_path, platform_name, architecture, required=()):
    worker = WorkloadWorker.__new__(WorkloadWorker)
    worker.config = {'required_handler_releases': list(required)}
    worker.base_handlers = {'test.v1': {'backend': 'native', 'argv': ['x']}}
    worker.handlers = dict(worker.base_handlers)
    worker.handler_packages = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        worker.base_handlers, platform_name=platform_name, architecture=architecture)
    worker.handler_registry_failures, worker.handler_platform_skips, worker.handler_gc_receipts = [], [], []
    worker.handler_registry_state = worker.handler_packages.read_pointer()
    worker.handler_inventory = worker.handler_packages.inventory(0)
    # Any download attempt is a bug in these tests: the skip must precede the transfer.
    worker.transfer = None
    worker.journal = type('J', (), {'read': staticmethod(lambda: None)})()
    return worker


def _sync(bundle, generation=73):
    manifest, digest, archive, archive_digest = bundle
    descriptor = dict(handler_id='test.v1', release_digest=digest, archive_digest=archive_digest,
                      archive_bytes=archive.stat().st_size, manifest=manifest)
    return {'generation': generation, 'defaults': {'test.v1': digest}, 'releases': [descriptor],
            'retention_seconds': None, 'references_complete': True}, digest


def test_foreign_platform_default_is_skipped_by_name_and_generation_advances(tmp_path):
    bundle = package(tmp_path, 'skip-a')
    manifest = bundle[0]
    other = ('darwin', 'aarch64') if (manifest['platform'], manifest['architecture']) != ('darwin', 'aarch64') \
        else ('linux', 'x86_64')
    worker = _worker(tmp_path, *other)
    sync, digest = _sync(bundle)

    worker._sync_handler_registry(sync)

    assert worker.handler_registry_failures == []
    assert worker.handler_platform_skips == [dict(
        generation=73, handler_id='test.v1', release_digest=digest, reason='platform_not_supported',
        release_platform=manifest['platform'], release_architecture=manifest['architecture'])]
    assert worker.handler_registry_state == {'generation': 73, 'defaults': {}}
    assert worker.handler_inventory['generation'] == 73
    assert worker.handlers['test.v1'] == worker.base_handlers['test.v1']
    assert not (tmp_path/'worker-state'/digest).exists()  # never downloaded or installed


def test_required_release_for_a_foreign_platform_still_fails_loudly(tmp_path):
    bundle = package(tmp_path, 'skip-b')
    manifest = bundle[0]
    other = ('darwin', 'aarch64') if (manifest['platform'], manifest['architecture']) != ('darwin', 'aarch64') \
        else ('linux', 'x86_64')
    worker = _worker(tmp_path, *other, required=['test.v1'])
    sync, digest = _sync(bundle)

    assert worker._sync_handler_registry(sync) is False

    assert worker.handler_platform_skips == []
    assert worker.handler_registry_failures[0]['reason'].startswith('required_release_platform_missing: platform_not_supported')
    assert worker.handler_registry_state['generation'] == 0


def test_matching_platform_is_installed_not_skipped(tmp_path):
    bundle = package(tmp_path, 'skip-c')
    manifest, digest, archive, _ = bundle
    worker = _worker(tmp_path, manifest['platform'], manifest['architecture'])
    sync, _ = _sync(bundle)
    worker.transfer = type('T', (), {'get': staticmethod(lambda _d, dest: dest.write_bytes(archive.read_bytes()))})()

    worker._sync_handler_registry(sync)

    assert worker.handler_platform_skips == []
    assert worker.handler_registry_state['defaults'] == {'test.v1': digest}


def test_authority_accepts_and_validates_the_skip_report(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    digest = hashlib.sha256(b'x').hexdigest()
    skip = dict(generation=73, handler_id='test.v1', release_digest=digest, reason='platform_not_supported',
                release_platform='linux', release_architecture='x86_64')
    report = dict(capacity={'cpu': 1, 'memory_bytes': 1 << 30, 'disk_bytes': 1 << 30},
                  available={'cpu': 1, 'memory_bytes': 1 << 30, 'disk_bytes': 1 << 30},
                  labels={}, handlers=['test.v1'], ready=True,
                  handler_inventory={'generation': 73, 'defaults': {}, 'releases': []},
                  handler_activation_failures=[], handler_platform_skips=[skip])
    store.register('w1', 'host1', 'boot1', report)
    with pytest.raises(Exception, match='handler_platform_skips_invalid'):
        store.register('w1', 'host1', 'boot1', dict(report, handler_platform_skips=[dict(skip, reason='boom')]))


def test_inventory_omits_packages_of_a_handler_the_worker_no_longer_serves(tmp_path):
    manifest, digest, archive, archive_digest = package(tmp_path, 'inv-a')
    descriptor = dict(manifest=manifest, release_digest=digest, archive_digest=archive_digest,
                      archive_bytes=archive.stat().st_size)
    serving = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'test.v1': {'backend': 'native'}}, platform_name=manifest['platform'], architecture=manifest['architecture'])
    serving.install(archive, descriptor)
    assert [r['release_digest'] for r in serving.inventory(1)['releases']] == [digest]
    dropped = HandlerPackageStore(tmp_path/'worker-state', {'node22': sys.executable},
        {'other.v1': {'backend': 'native'}}, platform_name=manifest['platform'], architecture=manifest['architecture'])
    assert dropped.inventory(1)['releases'] == []
