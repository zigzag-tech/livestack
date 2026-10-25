"""The general package builder produces exactly the registry's identity, and refuses what the registry would."""
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from livestack_node.workloads.handler_installer import HandlerPackageStore
from livestack_node.workloads.handler_package_builder import build_bundle, build_release_package, main
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.model import WorkloadError

FIXTURE = Path(__file__).parent/'fixtures'/'harmony-handler-release-v1'
FIXTURE_DIGEST = '0c86a76fb84316c50bf284c4e7b53b3ff9767febc631c777d2c6799e1658f93b'
FIELDS = dict(handler_id='benchday.fixture.e2e', release_version='1.0.0', runtime_id='node22',
              entrypoint='handler.mjs', payload_schema='benchday.test-train.request.v1',
              result_schema='benchday.test-train.result.v1', platform='linux', architecture='x86_64',
              outputs=['e2e-result.json'], infrastructure_exit_codes=[10, 12])


@pytest.fixture
def source(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    shutil.copy(FIXTURE/'handler.mjs', root/'handler.mjs')
    os.chmod(root/'handler.mjs', 0o644)
    return root


def build(source, tmp_path, **override):
    return build_release_package(source, tmp_path/'out', **{**FIELDS, **override})


def test_builder_reproduces_the_shared_fixture_identity(source, tmp_path):
    descriptor = build(source, tmp_path)
    assert descriptor['release_digest'] == FIXTURE_DIGEST
    assert descriptor['manifest'] == json.loads((FIXTURE/'manifest.json').read_text())
    assert validate_manifest(descriptor['manifest'], FIXTURE_DIGEST)['release_digest'] == FIXTURE_DIGEST
    archive = tmp_path/'out'/descriptor['archive_path']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == descriptor['archive_digest']


def test_archive_is_deterministic_and_installs_on_a_worker_store(source, tmp_path):
    first = build(source, tmp_path)
    second = build_release_package(source, tmp_path/'again', **FIELDS)
    assert first['archive_digest'] == second['archive_digest']
    archive = tmp_path/'out'/first['archive_path']
    store = HandlerPackageStore(tmp_path/'worker', {'node22': sys.executable}, {},
                                platform_name='linux', architecture='x86_64')
    installed = store.install(archive, dict(manifest=first['manifest'], release_digest=first['release_digest'],
        archive_digest=first['archive_digest'], archive_bytes=archive.stat().st_size))
    assert store.verify_installed(first['release_digest']) == first['manifest']
    assert (installed/'payload'/'handler.mjs').read_bytes() == (source/'handler.mjs').read_bytes()


def test_executable_bit_maps_to_the_normalized_mode(source, tmp_path):
    os.chmod(source/'handler.mjs', 0o755)
    assert build(source, tmp_path)['manifest']['files'][0]['mode'] == 0o555


def test_bundle_matches_the_layout_the_stage_command_reads(source, tmp_path):
    (source/'extra.txt').write_text('x')
    releases = build_bundle(source, tmp_path/'bundle', release_version='r1', platform='linux',
        architecture='x86_64', allowed_runtime_ids=('node22',), handlers={
            'one.v1': dict(runtime_id='node22', entrypoint='handler.mjs', arguments=['one'],
                           payload_schema='one.request.v1', result_schema='one.result.v1'),
            'two.v1': dict(runtime_id='node22', entrypoint='handler.mjs', arguments=['two'],
                           payload_schema='two.request.v1', result_schema='two.result.v1')})
    written = json.loads((tmp_path/'bundle'/'manifest.json').read_text())
    assert written == {'handler_releases': releases}
    assert set(releases) == {'one.v1', 'two.v1'}
    assert releases['one.v1']['release_digest'] != releases['two.v1']['release_digest']
    for handler, item in releases.items():
        archive = tmp_path/'bundle'/item['archive_path']
        package = json.loads(archive.with_suffix('.json').read_text())
        assert package['archive_digest'] == item['archive_digest']
        validate_manifest(package['manifest'], item['release_digest'])


def test_symlink_is_refused(source, tmp_path):
    os.symlink('/etc/hostname', source/'link')
    with pytest.raises(WorkloadError, match='handler_package_link_refused:link'):
        build(source, tmp_path)


def test_special_file_is_refused(source, tmp_path):
    os.mkfifo(source/'pipe')
    with pytest.raises(WorkloadError, match='handler_package_special_file_refused:pipe'):
        build(source, tmp_path)


def test_unknown_runtime_is_refused(source, tmp_path):
    with pytest.raises(WorkloadError, match='handler_runtime_not_operator_installed'):
        build(source, tmp_path, runtime_id='/usr/bin/node')
    with pytest.raises(WorkloadError, match='handler_runtime_not_operator_installed'):
        build(source, tmp_path, runtime_id='bash')


def test_undeclared_file_is_left_out_and_a_missing_declared_file_is_refused(source, tmp_path):
    (source/'undeclared.txt').write_text('not part of the closure')
    descriptor = build(source, tmp_path, include_paths=['handler.mjs'])
    assert [item['path'] for item in descriptor['manifest']['files']] == ['handler.mjs']
    with pytest.raises(WorkloadError, match='handler_package_closure_missing:absent.mjs'):
        build(source, tmp_path, include_paths=['handler.mjs', 'absent.mjs'])


def test_entrypoint_must_be_a_packaged_file(source, tmp_path):
    with pytest.raises(WorkloadError, match='handler_manifest_entrypoint_missing'):
        build(source, tmp_path, entrypoint='missing.mjs')
    with pytest.raises(WorkloadError, match='handler_manifest_invalid_entrypoint'):
        build(source, tmp_path, entrypoint='../handler.mjs')


def test_output_directory_inside_the_package_root_is_refused(source, tmp_path):
    with pytest.raises(WorkloadError, match='handler_package_output_inside_root'):
        build_release_package(source, source/'out', **FIELDS)


def test_outputs_are_sorted_and_deduplicated_into_canonical_form(source, tmp_path):
    descriptor = build(source, tmp_path, outputs=['b.json', 'a.json', 'b.json'], infrastructure_exit_codes=[12, 10, 10])
    assert descriptor['manifest']['outputs'] == ['a.json', 'b.json']
    assert descriptor['manifest']['infrastructure_exit_codes'] == [10, 12]


def test_noncanonical_manifest_is_refused_by_the_registry_validator(source, tmp_path):
    manifest = build(source, tmp_path)['manifest']
    manifest['outputs'] = ['b.json', 'a.json']
    with pytest.raises(WorkloadError, match='handler_manifest_noncanonical_outputs'):
        validate_manifest(manifest)


def test_extra_member_in_the_archive_is_refused_at_install(source, tmp_path):
    descriptor = build(source, tmp_path)
    archive = tmp_path/'out'/descriptor['archive_path']
    tampered = tmp_path/'tampered.tar'
    shutil.copy(archive, tampered)
    data = tampered.read_bytes()[:-1024]
    with tarfile.open(tmp_path/'extra.tar', 'w', format=tarfile.USTAR_FORMAT) as extra:
        info = tarfile.TarInfo('undeclared.txt')
        info.size = 1
        import io
        extra.addfile(info, io.BytesIO(b'x'))
    tampered.write_bytes(data + (tmp_path/'extra.tar').read_bytes())
    store = HandlerPackageStore(tmp_path/'worker', {'node22': sys.executable}, {},
                                platform_name='linux', architecture='x86_64')
    with pytest.raises(WorkloadError, match='handler_archive_inventory_mismatch'):
        store.install(tampered, dict(manifest=descriptor['manifest'], release_digest=descriptor['release_digest'],
            archive_digest=hashlib.sha256(tampered.read_bytes()).hexdigest(), archive_bytes=tampered.stat().st_size))


def test_command_line_builds_a_bundle_and_refuses_with_a_named_reason(source, tmp_path, capsys):
    spec = tmp_path/'spec.json'
    spec.write_text(json.dumps(dict(release_version='r1', platform='linux', architecture='x86_64', handlers={
        'one.v1': dict(runtime_id='node22', entrypoint='handler.mjs', payload_schema='one.request.v1',
                       result_schema='one.result.v1')})))
    assert main(['--root', str(source), '--out', str(tmp_path/'cli'), '--spec', str(spec)]) == 0
    assert 'one.v1' in capsys.readouterr().out
    assert main(['--root', str(source), '--out', str(tmp_path/'cli2'), '--spec', str(spec),
                 '--runtime-id', 'python3']) == 2
    assert 'handler_runtime_not_operator_installed' in capsys.readouterr().err
