"""host_release.py against a real systemd user manager: a scratch worker unit is switched to another release
directory and back. Skipped where there is no user manager. Every file it creates is removed."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from livestack_node.workloads import host_release

pytestmark = pytest.mark.skipif(
    subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True).returncode != 0,
    reason='no systemd user manager')
UNIT_DIR = Path.home() / '.config/systemd/user'


def make_release(root, name, body):
    pkg = root / name / 'node-py' / 'livestack_node'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text(body)
    return str(root / name), host_release.content_hash(str(root / name))


@pytest.fixture
def scratch(tmp_path):
    worker, unit = f'rolltest-{os.getpid()}', f'livestack-workload-worker-rolltest-{os.getpid()}'
    releases = tmp_path / 'releases'
    (releases).mkdir()
    _, hash_a = make_release(releases, 'rel-a', 'A = 1\n')
    _, hash_b = make_release(releases, 'rel-b', 'A = 2\n')
    state = tmp_path / 'state'
    state.mkdir()
    cfg = tmp_path / 'worker.json'
    cfg.write_text(json.dumps(dict(worker=worker, state_dir=str(state))))
    (UNIT_DIR / f'{unit}.service').write_text(
        '[Service]\nExecStart=/usr/bin/python3 -c "import time; time.sleep(100000)" worker_service --config %s\n'
        'Environment=PYTHONPATH=%s:%s\n' % (cfg, releases / 'rel-a' / 'node-py', tmp_path / 'deps'))
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'start', unit + '.service'], check=True)
    try:
        yield dict(worker=worker, unit=unit, releases=str(releases), hash_a=hash_a, hash_b=hash_b, state=state, tmp=tmp_path)
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit + '.service'], capture_output=True)
        os.remove(UNIT_DIR / f'{unit}.service')
        shutil.rmtree(UNIT_DIR / f'{unit}.service.d', ignore_errors=True)
        shutil.rmtree(Path(host_release.STATE_ROOT) / unit, ignore_errors=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'])


def op(name, args):
    p = subprocess.run([sys.executable, host_release.__file__, name, json.dumps(args)], capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


def test_switch_replaces_only_the_release_entry_and_rollback_restores(scratch):
    s = scratch
    rc, st = op('status', dict(worker=s['worker']))
    assert rc == 0 and st['release'] == 'rel-a' and st['content_hash'] == s['hash_a'] and not st['managed']
    rc, sw = op('switch', dict(worker=s['worker'], release='rel-b', content_hash=s['hash_b'], releases_dir=s['releases']))
    assert rc == 0, sw
    assert sw['new_pythonpath'] == f"{s['releases']}/rel-b/node-py:{s['tmp']}/deps", 'the dependency entry is kept'
    rc, st = op('status', dict(worker=s['worker']))
    assert (st['release'], st['managed'], st['content_hash']) == ('rel-b', True, s['hash_b'])
    assert st['main_pid'] == sw['main_pid'] and st['active'] == 'active'
    rc, rb = op('rollback', dict(worker=s['worker'], release='rel-b'))
    assert rc == 0, rb
    rc, st = op('status', dict(worker=s['worker']))
    assert (st['release'], st['managed']) == ('rel-a', False)


def test_refusals_change_nothing(scratch):
    s = scratch
    rc, r = op('switch', dict(worker=s['worker'], release='rel-b', content_hash='0' * 64, releases_dir=s['releases']))
    assert (rc, r['failure']) == (1, 'release_content_hash_mismatch')
    rc, r = op('switch', dict(worker=s['worker'], release='absent', content_hash=s['hash_b'], releases_dir=s['releases']))
    assert (rc, r['failure']) == (1, 'release_missing')
    (s['state'] / 'active.json').write_text('{}')
    rc, r = op('switch', dict(worker=s['worker'], release='rel-b', content_hash=s['hash_b'], releases_dir=s['releases']))
    assert (rc, r['failure']) == (1, 'worker_busy')
    rc, r = op('rollback', dict(worker=s['worker'], release='rel-b'))
    assert (rc, r['failure']) == (1, 'no_switch_record')
    rc, st = op('status', dict(worker=s['worker']))
    assert (st['release'], st['managed']) == ('rel-a', False)
    assert op('status', dict(worker='no-such-worker'))[1]['failure'] == 'unit_not_found'


def test_rollback_will_not_undo_a_different_roll(scratch):
    s = scratch
    assert op('switch', dict(worker=s['worker'], release='rel-b', content_hash=s['hash_b'], releases_dir=s['releases']))[0] == 0
    rc, r = op('rollback', dict(worker=s['worker'], release='rel-c'))
    assert (rc, r['failure']) == (1, 'switch_record_is_not_for_this_rollout')


def test_a_release_that_does_not_come_up_is_reported_not_healthy(scratch):
    s = scratch
    rel, h = make_release(Path(s['releases']), 'rel-bad', 'A = 3\n')
    (UNIT_DIR / f"{s['unit']}.service.d").mkdir(exist_ok=True)
    # make the next start fail: the unit now exits at once
    (UNIT_DIR / f"{s['unit']}.service.d" / '00-die.conf').write_text('[Service]\nExecStart=\nExecStart=/bin/false worker_service --config /nonexistent\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'])
    rc, r = op('switch', dict(worker=s['worker'], release='rel-bad', content_hash=h, releases_dir=s['releases'], health_seconds=6))
    assert (rc, r['failure']) == (1, 'not_healthy_on_new_release')
