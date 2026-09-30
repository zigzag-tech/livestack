"""Real directories, sockets and processes: cleanup of a runtime dir an OOM-killed attempt left."""
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from livestack_node.workloads.docker_runtime import RuntimeCleanupRefused, cleanup, prepare, runtime_path

UNIT = 'harmony-attempt-abc'


@pytest.fixture(autouse=True)
def base(tmp_path, monkeypatch):
    (tmp_path/'run').mkdir()
    monkeypatch.setenv('LIVESTACK_WORKLOAD_RUNTIME_BASE', str(tmp_path/'run'))


def leftover(unit=UNIT, owner=None):
    path = runtime_path(unit)
    path.mkdir(mode=0o700)
    os.mkfifo(path/'.bp-ready.pipe')
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path/'api.sock'))
    sock.close()  # a dead rootlesskit leaves the socket file, not the listener
    if owner is not None:
        (path/'owner.json').write_text(json.dumps(owner))
    return path


def test_dir_without_owner_json_is_removed_and_named(caplog):
    path = leftover()
    with caplog.at_level(logging.WARNING):
        cleanup(UNIT)
    assert not path.exists()
    lines = [r.getMessage() for r in caplog.records if 'docker runtime cleanup' in r.getMessage()]
    assert lines == ['docker runtime cleanup: removed %s (no owner.json: attempt died before writing it)' % path]


def test_dir_with_matching_or_garbled_marker_is_removed():
    path = leftover(owner={'unit': UNIT})
    cleanup(UNIT)
    assert not path.exists()
    path = leftover()
    (path/'owner.json').write_text('{not json')
    cleanup(UNIT)
    assert not path.exists()


def test_other_units_marker_is_still_refused():
    path = leftover(owner={'unit': 'someone-else'})
    with pytest.raises(RuntimeCleanupRefused, match='owner mismatch'):
        cleanup(UNIT)
    assert path.exists()


def test_symlinked_runtime_path_is_refused(tmp_path):
    target = tmp_path/'elsewhere'
    target.mkdir()
    (target/'keep').write_text('x')
    runtime_path(UNIT).symlink_to(target)
    with pytest.raises(RuntimeCleanupRefused, match='unrecognized'):
        cleanup(UNIT)
    assert (target/'keep').exists()


def test_dir_a_live_process_uses_is_not_removed_until_it_exits():
    path = leftover()
    proc = subprocess.Popen(['sleep', '30'], cwd=path)
    try:
        with pytest.raises(RuntimeCleanupRefused, match='in use by pid %d' % proc.pid):
            cleanup(UNIT)
        assert path.exists()
    finally:
        proc.kill()
        proc.wait()
    cleanup(UNIT)
    assert not path.exists()


def test_prepare_writes_owner_marker_and_refuses_reuse(tmp_path):
    out = tmp_path/'out'
    out.mkdir()
    prepare(UNIT, ['/bin/true'], tmp_path, out)
    assert json.loads((runtime_path(UNIT)/'owner.json').read_text()) == {'unit': UNIT}
    assert not (runtime_path(UNIT)/'.owner.json.tmp').exists()
    with pytest.raises(FileExistsError):
        prepare(UNIT, ['/bin/true'], tmp_path, out)
    cleanup(UNIT)
    assert not runtime_path(UNIT).exists()
