"""tools/check-authority-release.py against real releases: real processes, real HTTP, no fakes."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT/'tools'/'check-authority-release.py'
NODE_PY = ROOT/'node-py'


def run(release):
    return subprocess.run([sys.executable, str(CHECK), str(release), '--python', sys.executable],
                          capture_output=True, text=True, timeout=180)


def release_copy(tmp_path):
    """A release directory shaped like a deployed overlay: node-py/livestack_node (+ vendored deps if any)."""
    target = tmp_path/'release'/'node-py'
    shutil.copytree(NODE_PY/'livestack_node', target/'livestack_node',
                    ignore=shutil.ignore_patterns('__pycache__'))
    if (NODE_PY/'_deps').is_dir():
        shutil.copytree(NODE_PY/'_deps', target/'_deps', ignore=shutil.ignore_patterns('__pycache__'))
    return target


def test_a_good_release_passes_every_stage_and_prints_no_token(tmp_path):
    done = run(release_copy(tmp_path).parent)
    assert done.returncode == 0, done.stdout + done.stderr
    for stage in ('static', 'boot', 'worker', 'job'):
        assert f'PASS  {stage}' in done.stdout
    assert 'RESULT: PASS' in done.stdout
    assert 'Bearer' not in done.stdout + done.stderr


def test_a_missing_import_in_worker_registration_fails_with_the_named_cause(tmp_path):
    """The 2026-10-05 production failure: store.py used `re` without importing it, so the authority
    booted and answered status but every worker registration returned 503."""
    node_py = release_copy(tmp_path)
    store = node_py/'livestack_node'/'workloads'/'store.py'
    source = store.read_text()
    assert '\nimport re\n' in source
    store.write_text(source.replace('\nimport re\n', '\n', 1))
    done = run(node_py.parent)
    assert done.returncode == 1, done.stdout
    assert "undefined name 're'" in done.stdout                      # static stage names the file and name
    assert 'PASS  boot' in done.stdout                                # it really does boot
    assert 'FAIL  worker  [registration_http_503' in done.stdout      # the worker stage reproduces the 503
    assert 'RESULT: FAIL' in done.stdout


def test_a_release_that_does_not_boot_is_named(tmp_path):
    node_py = release_copy(tmp_path)
    service = node_py/'livestack_node'/'workloads'/'service.py'
    service.write_text(service.read_text().replace('def main():', 'def main():\n    raise SystemExit("scratch boot failure")', 1))
    done = run(node_py.parent)
    assert done.returncode == 1
    assert 'FAIL  boot  [authority_exited_' in done.stdout and 'scratch boot failure' in done.stdout


def test_a_directory_that_is_not_a_release_is_refused(tmp_path):
    done = run(tmp_path)
    assert done.returncode != 0 and 'not_a_release_dir' in done.stdout + done.stderr
