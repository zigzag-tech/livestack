"""tools/stage-handler-release.py against a real authority (HTTP + SQLite), as an operator runs it."""
import hashlib
import io
import json
import platform
import subprocess
import sys
import tarfile
from pathlib import Path
from threading import Thread

import pytest

from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore

TOOL = Path(__file__).resolve().parents[2]/'tools'/'stage-handler-release.py'
ADMIN = 'o'*32


def package(tmp_path):
    source = b"print('hi')\n"
    manifest = dict(format='harmony-handler-package.v1', handler_id='native.v1', release_version='1.0.0',
        execution_contract=1, payload_schema='x.request.v1', result_schema='x.result.v1', platform='linux',
        architecture={'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'arm64'}[platform.machine().lower()],
        backend='native', runtime_id='python3', entrypoint='handler.py', arguments=[], outputs=['out.json'],
        infrastructure_outputs=[], infrastructure_exit_codes=[75],
        files=[dict(path='handler.py', mode=0o444, size=len(source), sha256=hashlib.sha256(source).hexdigest())])
    release = validate_manifest(manifest)['release_digest']
    directory = tmp_path/'pkg'/'handler-packages'
    directory.mkdir(parents=True)
    archive = directory/'h.tar'
    with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo('handler.py')
        info.mode, info.size, info.mtime, info.uid, info.gid = 0o444, len(source), 0, 0, 0
        info.uname = info.gname = ''
        tar.addfile(info, io.BytesIO(source))
    descriptor = directory/'h.json'
    descriptor.write_text(json.dumps(dict(handler_id='native.v1', release_digest=release,
        archive_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
        archive_path='handler-packages/h.tar', manifest=manifest)))
    return descriptor, release


@pytest.fixture
def authority(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'})
    policy = dict(revision='t', handlers={'native.v1': dict(runtime_ids=['python3'], backends=['native'])})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal('operator', ADMIN, 'admin', ('native.v1',))],
                            handler_release_policy=policy)
    Thread(target=server.serve_forever, daemon=True).start()
    config = tmp_path/'authority.json'
    config.write_text(json.dumps(dict(bind='127.0.0.1', port=server.server_port,
        principals=[dict(id='operator', token=ADMIN, role='admin', handlers=['native.v1'])])))
    yield server, config
    server.shutdown()
    server.server_close()


def run(config, descriptor, *extra):
    return subprocess.run([sys.executable, str(TOOL), '--config', str(config), '--descriptor', str(descriptor), *extra],
                          capture_output=True, text=True)


def test_stage_then_activate_makes_the_release_the_default_and_never_prints_the_token(authority, tmp_path):
    server, config = authority
    descriptor, release = package(tmp_path)
    result = run(config, descriptor, '--activate')
    assert result.returncode == 0, result.stderr
    assert ADMIN not in result.stdout + result.stderr
    assert server.handler_registry.status()['defaults'] == {'native.v1': release}
    again = run(config, descriptor, '--activate')  # staging is idempotent
    assert again.returncode == 0, again.stderr


def test_a_handler_outside_the_policy_is_refused_by_name(authority, tmp_path):
    server, config = authority
    descriptor, _ = package(tmp_path)
    server.handler_registry.replace_policy(dict(handlers={}), server.store.handlers)
    result = run(config, descriptor)
    assert result.returncode == 1 and 'handler_release_policy_refused' in result.stderr
