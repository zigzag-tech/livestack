"""Smoke probes against fixtures that reproduce the four incident classes of 2026-10-07/08.

openspec/changes/declarative-worker-rollout, tasks 5.1-5.2 (framework + probes; the job runner that
pins them to a worker belongs to enforce mode and is not part of this release).
"""
import hashlib
import json
import os
import shutil
import stat
import textwrap

import pytest

from livestack_node.workloads import smoke, unit
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.model import WorkloadError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_package(root, files, *, entrypoint=None, omit=(), extra=None, handler='demo.v1'):
    """An installed handler package exactly as handler_installer lays it out:
    <root>/<digest>/.manifest.json and <root>/<digest>/payload/<files>. `omit` files are listed in the
    manifest but never written (the placeholder-removed class); `extra` are written but not listed."""
    listed = [dict(path=path, mode=0o444, size=len(data), sha256=sha(data)) for path, data in sorted(files.items())]
    manifest = dict(format='harmony-handler-package.v1', handler_id=handler, release_version='1',
                    execution_contract=1, payload_schema='demo-payload.v1', result_schema='demo-result.v1',
                    platform='linux', architecture='x86_64', backend='native', runtime_id='python3',
                    entrypoint=entrypoint or sorted(files)[0], arguments=[], outputs=[],
                    infrastructure_outputs=[], infrastructure_exit_codes=[], files=listed)
    checked = validate_manifest(manifest)
    package = root/checked['release_digest']
    payload = package/'payload'
    payload.mkdir(parents=True)
    for path, data in {**files, **(extra or {})}.items():
        if path in omit:
            continue
        target = payload.joinpath(*path.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
    (package/'.manifest.json').write_text(json.dumps(dict(manifest=manifest, release_digest=checked['release_digest'])))
    return checked['release_digest']


GOOD_PY = textwrap.dedent('''
    import json
    CACHE = {}

    def components(spec):
        cache = CACHE.setdefault('c', [])
        return [json.dumps(x) for x in spec] + cache

    if __name__ == "__main__":
        print(components([1]))
''').encode()


def test_good_bundle_passes_import_and_integrity(tmp_path):
    digest = make_package(tmp_path, {'run.py': GOOD_PY})
    ctx = dict(handlers_root=str(tmp_path))
    assert smoke.handler_import(ctx)['status'] == 'pass'
    assert smoke.handler_integrity(dict(ctx, digests=[digest]))['status'] == 'pass'


# Incident (c): `cacheComponents is not defined` found only after 25 s of a real attempt.
def test_undefined_identifier_is_caught_in_milliseconds_not_at_run_time(tmp_path):
    bad = GOOD_PY.replace(b'cache = CACHE.setdefault', b'cache = cacheComponents.setdefault')
    make_package(tmp_path, {'run.py': bad})
    result = smoke.handler_import(dict(handlers_root=str(tmp_path)))
    assert result['status'] == 'fail' and result['failure'].startswith('smoke_failed:handler_import:')
    assert 'undefined_name' in result['reason'] and 'cacheComponents' in result['reason']
    assert result['elapsed_s'] < 2


# Incident (c): `workerCache` redeclared. Python spelling: a def shadowed by a second def.
def test_redeclared_identifier_is_caught(tmp_path):
    bad = GOOD_PY + b'\ndef components(spec):\n    return []\n'
    make_package(tmp_path, {'run.py': bad})
    result = smoke.handler_import(dict(handlers_root=str(tmp_path)))
    assert result['status'] == 'fail' and 'redeclared' in result['reason'] and 'components' in result['reason']


@pytest.mark.skipif(shutil.which('node') is None, reason='needs node')
def test_js_redeclaration_is_a_syntax_error_node_check_reports(tmp_path):
    js = b'const workerCache = new Map();\nfunction f() { return workerCache; }\nconst workerCache = 1;\n'
    make_package(tmp_path, {'run.js': js})
    result = smoke.handler_import(dict(handlers_root=str(tmp_path)))
    assert result['status'] == 'fail' and 'already been declared' in json.dumps(result)


def test_syntax_error_is_caught(tmp_path):
    make_package(tmp_path, {'run.py': b'def broken(:\n    pass\n'})
    assert smoke.handler_import(dict(handlers_root=str(tmp_path)))['reason'].find('syntax_error') >= 0


def test_star_import_is_not_judged(tmp_path):
    make_package(tmp_path, {'run.py': b'from os.path import *\nprint(join("a", undefined_here))\n'})
    assert smoke.handler_import(dict(handlers_root=str(tmp_path)))['status'] == 'pass'


# Incident (c): the placeholder was removed before the integrity check ran against the bundle as shipped.
def test_removed_placeholder_fails_integrity(tmp_path):
    digest = make_package(tmp_path, {'run.py': GOOD_PY, 'assets/PLACEHOLDER': b'x'}, omit=('assets/PLACEHOLDER',))
    result = smoke.handler_integrity(dict(handlers_root=str(tmp_path), digests=[digest]))
    assert result['status'] == 'fail' and result['reason'].endswith('missing:assets/PLACEHOLDER')


def test_unlisted_file_and_tampered_file_fail_integrity(tmp_path):
    digest = make_package(tmp_path, {'run.py': GOOD_PY}, extra={'stray.txt': b'?'})
    assert smoke.handler_integrity(dict(handlers_root=str(tmp_path)))['reason'].endswith('unlisted:stray.txt')
    other = tmp_path/'o'; other.mkdir()
    digest = make_package(other, {'run.py': GOOD_PY})
    target = other/digest/'payload'/'run.py'
    target.chmod(0o644); target.write_bytes(GOOD_PY.replace(b'CACHE', b'CACHF')); target.chmod(0o444)
    assert smoke.handler_integrity(dict(handlers_root=str(other)))['reason'].endswith('digest:run.py')


def test_no_bundle_is_not_applicable_never_pass(tmp_path):
    assert smoke.handler_import(dict(handlers_root=str(tmp_path/'absent')))['status'] == 'not_applicable'
    assert smoke.handler_integrity(dict(handlers_root=str(tmp_path)))['status'] == 'not_applicable'


# Incident (c): PrivateTmp=yes broke newuidmap for rootless docker on --user-manager hosts.
def fake_docker(tmp_path, script):
    path = tmp_path/'docker'
    path.write_text('#!/bin/sh\n' + script)
    path.chmod(0o755)
    return str(path)


def test_rootless_docker_newuidmap_failure_is_named(tmp_path):
    docker = fake_docker(tmp_path, 'echo "newuidmap: write to uid_map failed: Operation not permitted" >&2\n'
                                   'echo "Error: cannot set up user namespace" >&2\nexit 125\n')
    result = smoke.rootless_docker_start(dict(docker=docker, image='alpine'))
    assert result['status'] == 'fail' and result['failure'] == 'smoke_failed:rootless_docker_start:newuidmap_denied'


def test_rootless_docker_passes_and_is_not_applicable_without_it(tmp_path):
    ok = fake_docker(tmp_path, 'exit 0\n')
    assert smoke.rootless_docker_start(dict(docker=ok, image='alpine'))['status'] == 'pass'
    assert smoke.rootless_docker_start(dict(docker=ok))['reason'] == 'no_probe_image_named'
    assert smoke.rootless_docker_start(dict(docker=str(tmp_path/'nodocker'), image='x'))['status'] == 'not_applicable'
    missing = fake_docker(tmp_path, 'echo "Unable to find image" >&2; exit 125\n')
    assert smoke.rootless_docker_start(dict(docker=missing, image='x'))['status'] == 'not_applicable'


def test_rootless_docker_runs_inside_the_given_sandbox_wrapper(tmp_path):
    """`wrap` is how the probe enters the worker's own mount namespace (nsenter -t PID -m)."""
    docker = fake_docker(tmp_path, 'exit 0\n')
    marker = tmp_path/'wrapped'
    wrapper = tmp_path/'wrap'
    wrapper.write_text(f'#!/bin/sh\ntouch {marker}\nexec "$@"\n'); wrapper.chmod(0o755)
    assert smoke.rootless_docker_start(dict(docker=docker, image='x', wrap=[str(wrapper)]))['status'] == 'pass'
    assert marker.exists()


# Incident (b): a stale or missing root-owned verifier copy.
def test_stale_or_missing_verifier_copy_fails(tmp_path):
    payload = tmp_path/'payload'; payload.mkdir()
    (payload/'verifier.py').write_text('print(1)\n')
    want = smoke.tree_digest(payload)
    copy = tmp_path/'copy'; shutil.copytree(payload, copy)
    assert smoke.compilation_launch(dict(verifier_dir=str(copy), verifier_digest=want))['status'] == 'pass'
    (copy/'verifier.py').write_text('print(0)\n')
    stale = smoke.compilation_launch(dict(verifier_dir=str(copy), verifier_digest=want))
    assert stale['failure'] == 'smoke_failed:compilation_launch:verifier_copy_stale'
    assert smoke.compilation_launch(dict(verifier_dir=str(tmp_path/'none'), verifier_digest=want))['reason'] == \
        'verifier_copy_missing'
    assert smoke.compilation_launch(dict())['status'] == 'not_applicable'
    assert smoke.compilation_launch(dict(verifier_dir=str(payload), verifier_digest=want,
                                         launch_cmd=['false']))['reason'] == 'noop_launch_failed'


# Incident (d): a runtime capture over the cap.
def test_capture_over_cap_fails_and_undeclared_cap_is_not_a_pass(tmp_path):
    big = tmp_path/'capture'; big.mkdir()
    (big/'a.bin').write_bytes(b'x' * 600)
    assert smoke.capture_size(dict(capture_path=str(big), cap_bytes=1000))['status'] == 'pass'
    over = smoke.capture_size(dict(capture_path=str(big), cap_bytes=500))
    assert over['failure'] == 'smoke_failed:capture_size:over_cap:600>500'
    assert smoke.capture_size(dict(size_bytes=1))['reason'] == 'cap_undeclared'
    assert smoke.capture_size(dict())['status'] == 'not_applicable'


# worker_restart_clean, against a systemctl fake (the real one needs a unit).
def fake_systemctl(states):
    calls = iter(states)

    def run(args):
        state = next(calls)
        return ''.join(f'{k}={v}\n' for k, v in state.items())
    return run


RUNNING = dict(ActiveState='active', SubState='running', NRestarts='0', MainPID='42', Result='success')


def test_restart_clean_detects_crash_loop_and_dead_unit():
    ok = smoke.worker_restart_clean(dict(unit='w', window_s=0, systemctl=fake_systemctl([RUNNING, RUNNING])))
    assert ok['status'] == 'pass'
    loop = smoke.worker_restart_clean(dict(unit='w', window_s=0, systemctl=fake_systemctl(
        [RUNNING, dict(RUNNING, NRestarts='3', MainPID='99')])))
    assert loop['failure'] == 'smoke_failed:worker_restart_clean:crash_loop'
    dead = smoke.worker_restart_clean(dict(unit='w', window_s=0, systemctl=fake_systemctl(
        [RUNNING, dict(RUNNING, ActiveState='failed', SubState='failed', Result='exit-code')])))
    assert dead['reason'].startswith('not_running:failed')
    assert smoke.worker_restart_clean(dict(window_s=0))['status'] == 'not_applicable'


# The runner: unknown is not success.
def test_runner_requires_the_minimum_to_actually_pass(tmp_path):
    digest = make_package(tmp_path, {'run.py': GOOD_PY})
    ctx = dict(handlers_root=str(tmp_path), unit='w', window_s=0, systemctl=fake_systemctl([RUNNING, RUNNING]))
    outcome = smoke.run([], ctx)
    assert outcome['ok'] and outcome['minimum_unmet'] == []
    ctx = dict(handlers_root=str(tmp_path/'absent'), unit='w', window_s=0, systemctl=fake_systemctl([RUNNING, RUNNING]))
    outcome = smoke.run([], ctx)
    assert not outcome['ok'] and outcome['minimum_unmet'] == ['handler_import', 'handler_integrity']
    assert outcome['failures'] == []   # nothing failed; it simply was not shown to work


def test_runner_turns_a_probe_crash_into_a_named_failure():
    outcome = smoke.run(['capture_size'], dict(size_bytes='not a number', cap_bytes=1))
    crashed = next(r for r in outcome['results'] if r['probe'] == 'capture_size')
    assert crashed['status'] == 'fail' and crashed['reason'].startswith('probe_error:')


def test_cli_exit_code_follows_the_outcome(tmp_path):
    make_package(tmp_path, {'run.py': GOOD_PY})
    context = tmp_path/'ctx.json'
    context.write_text(json.dumps(dict(handlers_root=str(tmp_path), size_bytes=1, cap_bytes=2)))
    assert smoke.main(['--context', str(context), '--probe', 'capture_size']) == 1  # restart probe not applicable


# ---- deployment unit ---------------------------------------------------------------------------------

def release_dir(tmp_path):
    release = tmp_path/'rel'/'node-py'
    release.mkdir(parents=True)
    (release/'RELEASE.json').write_text(json.dumps(dict(commit='a'*40, content_hash='b'*64, files=3)))
    return tmp_path/'rel'


def test_unit_builds_with_stable_digest_and_refuses_each_missing_part(tmp_path):
    handlers = tmp_path/'handlers'; handlers.mkdir()
    digest = make_package(handlers, {'run.py': GOOD_PY})
    args = dict(release_dir=release_dir(tmp_path), handlers_root=handlers, digests=[digest], capture_size=10,
                capture_cap=100, min_authority='main', built_from=['a'*12])
    first, second = unit.build(**args), unit.build(**args)
    assert first == second and first['id'].startswith('unit-') and len(first['id']) == 13
    for broken, reason in (
            (dict(args, release_dir=tmp_path/'none'), 'unit_release_missing'),
            (dict(args, verifier_dir=tmp_path/'no-verifier'), 'unit_verifier_missing'),
            (dict(args, capture_size=101), 'smoke_failed:capture_size:over_cap'),
            (dict(args, digests=['c'*64]), 'smoke_failed:handler_integrity:'),
    ):
        with pytest.raises(WorkloadError) as error:
            unit.build(**broken)
        assert reason in str(error.value), (reason, str(error.value))


def test_unit_build_refuses_a_handler_that_does_not_import(tmp_path):
    handlers = tmp_path/'handlers'; handlers.mkdir()
    digest = make_package(handlers, {'run.py': GOOD_PY.replace(b'json.dumps', b'jsonn.dumps')})
    with pytest.raises(WorkloadError) as error:
        unit.build(release_dir=release_dir(tmp_path), handlers_root=handlers, digests=[digest], capture_size=1,
                   capture_cap=2, min_authority='main', built_from=['a'*12])
    assert 'smoke_failed:handler_import:' in str(error.value)


def test_unit_manifest_refuses_credentials_and_unknown_keys(tmp_path):
    handlers = tmp_path/'handlers'; handlers.mkdir()
    digest = make_package(handlers, {'run.py': GOOD_PY})
    good = unit.build(release_dir=release_dir(tmp_path), handlers_root=handlers, digests=[digest],
                      capture_size=1, capture_cap=2, min_authority='main', built_from=['a'*12])['manifest']
    for mutate in (lambda m: m.update(signing_key='x'), lambda m: m['built_from'].append('-----BEGIN PRIVATE KEY'),
                   lambda m: m.update(extra=1)):
        bad = json.loads(json.dumps(good)); mutate(bad)
        with pytest.raises(WorkloadError):
            unit.validate(bad)


def test_unit_state_table():
    desired = dict(release='r'*64, handlers=['h'*64], verifier='v'*64)
    same = dict(release='r'*64, handlers=['h'*64, 'z'*64], verifier='v'*64)
    cases = [
        (same, desired, ('current', [])),
        (None, desired, ('unknown', [])),
        (same, None, ('undeclared', [])),
        (dict(same, verifier='o'*64), desired, ('unit_mismatch:verifier', ['verifier'])),      # new bundle, old verifier
        (dict(same, release='o'*64, handlers=[]), desired, ('unit_mismatch:release,handlers', ['release', 'handlers'])),
        (dict(release='o'*64, handlers=[], verifier='o'*64), desired, ('behind', ['release', 'handlers', 'verifier'])),
        (dict(same, verifier=None), dict(desired, verifier=None), ('current', [])),             # unit without a verifier
    ]
    for reported, want, expected in cases:
        assert unit.unit_state(reported, want) == expected


def test_worker_report_is_closed_and_bounded():
    assert unit.validate_report(dict(release=None, handlers=[], verifier=None))
    for bad in (dict(release='x', handlers=[], verifier=None), dict(release=None, handlers=['x'], verifier=None),
                dict(release=None, handlers=[], verifier=None, extra=1), 'str'):
        with pytest.raises(WorkloadError):
            unit.validate_report(bad)
