"""The root launch verifier is a separate copy of the launch contract. Rolling a worker release
must show when that copy drifted, and a refusal must name the check that failed (Rule 13)."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from livestack_node.workloads import launch_contract
from livestack_node.workloads.model import WorkloadError

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'build-worker-release.py'
PKG = Path(launch_contract.__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location('build_worker_release', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_and_sdk(tmp_path):
    release, sdk = tmp_path/'release', tmp_path/'sdk'
    shutil.copytree(PKG, release/'node-py'/'livestack_node', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(PKG, sdk/'livestack_node', ignore=shutil.ignore_patterns('__pycache__'))
    return release, sdk


def test_identical_contract_modules_are_current(tmp_path, capsys):
    release, sdk = _release_and_sdk(tmp_path)
    assert _tool().verifier_drift(str(release), str(sdk), None) == 0
    assert 'VERIFIER SDK CURRENT' in capsys.readouterr().out


def test_a_drifted_or_missing_contract_module_is_named(tmp_path, capsys):
    release, sdk = _release_and_sdk(tmp_path)
    with open(sdk/'livestack_node'/'workloads'/'launch_contract.py', 'a') as handle:
        handle.write('\n# drift\n')
    (sdk/'livestack_node'/'workloads'/'launch_guard.py').unlink()
    assert _tool().verifier_drift(str(release), str(sdk), None) == 1
    out = capsys.readouterr().out
    assert 'differs  workloads/launch_contract.py' in out
    assert 'missing in SDK  workloads/launch_guard.py' in out
    assert 'VERIFIER SDK DRIFT: 2 of' in out


def test_untrusted_registry_names_the_failed_check(tmp_path):
    # Not root-owned and not under a root-owned chain: the refusal must say which directory/uid.
    registry = tmp_path/'registry.json'
    registry.write_text(json.dumps({'version': 1, 'slots': {}}))
    with pytest.raises(WorkloadError, match=r'compilation_registry_untrusted: .*registry\.json: directory .* has uid \d+ mode') as caught:
        launch_contract.trusted_json(registry)
    assert caught.value.status == 403


def test_unavailable_verification_names_the_failure_type(tmp_path):
    registry = tmp_path/'missing.json'
    # trusted_json on a missing file under a non-root dir refuses as untrusted first; use a root-owned dir.
    request = dict(version=1, worker='w', host='h', policy_revision='p', boot='b', job_id='j', attempt_id='a',
                   fence=1, input_digest='0'*64, **{'class': 'rust'})
    with pytest.raises(WorkloadError) as caught:
        launch_contract.verify_launch(request, registry_path='/etc/livestack/no-such-registry.json')
    assert str(caught.value).startswith('compilation_verification_unavailable: FileNotFoundError')
    assert caught.value.status == 503
