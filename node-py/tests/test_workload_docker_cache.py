"""Persistent Docker build cache: real files and flock, then real rootless dockerd.

The first group needs no Docker (real directories, real kernel locks, real
processes). The second drives the installed launcher through SystemdExecutor,
exactly as test_workload_docker.py does, because only a real dockerd can show a
hit, a bound or a dockerd that will not start on a bad root.
"""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest

from livestack_node.workloads import docker_cache
from livestack_node.workloads.docker_cache import Session, namespace, plan, settings
from livestack_node.workloads.docker_runtime import remove_data, run_in_userns
from livestack_node.workloads.supervision import SystemdExecutor

IMAGE = 'docker.m.daocloud.io/library/alpine@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d'


@pytest.fixture(autouse=True)
def purge_roots(tmp_path):
    """Sub-uid owned dockerd files cannot be removed by pytest's own cleanup."""
    yield
    if (tmp_path/'cache').exists():
        try:
            run_in_userns(['/bin/chmod', '-R', 'u+rwx', str(tmp_path/'cache')], timeout=120)
            run_in_userns(['/bin/rm', '-rf', str(tmp_path/'cache')], timeout=300)
        except Exception:
            pass


def make_plan(tmp_path, owner='principal-a', attempt=None, **over):
    base = dict(path=str(tmp_path/'cache'), max_bytes=10*1024**3, epoch=0, canary_every=0, max_growth=1.5)
    base.update(over)
    (tmp_path/'cache').mkdir(mode=0o700, exist_ok=True)
    return plan(base, owner, attempt or uuid.uuid4().hex)


def run_session(tmp_path, p, *, code=0, grow=0, fingerprint=None, kill=False):
    """One whole attempt's cache life, with `grow` bytes written into the root."""
    out = tmp_path/('out-'+p['attempt'])
    out.mkdir()
    if fingerprint is not None:
        (out/docker_cache.FINGERPRINT_FILE).write_text(json.dumps(fingerprint))
    session = Session(p, out)
    root = session.begin()
    if root is not None and grow:
        (root/'blob').write_bytes(os.urandom(grow))
    if kill:
        return session, root
    session.finish(code)
    return session, root


# ----------------------------------------------------------------- settings

def test_settings_disabled_unless_valid_and_enabled(tmp_path):
    good = dict(enabled=True, path=str(tmp_path), max_bytes=10**9, epoch=3, canary_every=5)
    value, why = settings(good)
    assert why == 'enabled' and value['epoch'] == 3 and value['max_growth'] == 3.0
    assert settings(None) == (None, 'not-configured')
    assert settings(dict(good, enabled=False)) == (None, 'disabled')
    for bad in (dict(good, path='relative'), dict(good, path='/a/../b'), dict(good, max_bytes=True),
                dict(good, max_bytes=10), dict(good, epoch=-1), dict(good, surprise=1), dict(good, enabled='yes'),
                dict(good, canary_every=1.5), 'text'):
        value, why = settings(bad)
        assert value is None and why.startswith('invalid'), (bad, why)


def test_namespaces_never_collide():
    assert namespace('a/b') != namespace('a-b') and namespace('x') == namespace('x')
    assert '/' not in namespace('../../etc')


# ------------------------------------------------- real files, real locks

def test_second_attempt_hits_and_first_is_cold(tmp_path):
    first, root = run_session(tmp_path, make_plan(tmp_path), grow=4096)
    assert first.result['outcome'] == 'cold-new' and first.result['cold_bytes'] > 0
    second, root2 = run_session(tmp_path, make_plan(tmp_path))
    assert second.result['outcome'] == 'hit' and root2 == root and (root2/'blob').exists()


def test_principals_get_different_roots(tmp_path):
    _, a = run_session(tmp_path, make_plan(tmp_path, owner='a'), grow=100)
    second, b = run_session(tmp_path, make_plan(tmp_path, owner='b'))
    assert a != b and second.result['outcome'] == 'cold-new' and not (b/'blob').exists()


def test_busy_lock_runs_ephemeral_and_never_shares(tmp_path):
    p1, p2 = make_plan(tmp_path), make_plan(tmp_path)
    holder = Session(p1, tmp_path/'o1')
    (tmp_path/'o1').mkdir()
    (tmp_path/'o2').mkdir()
    assert holder.begin() is not None
    other = Session(p2, tmp_path/'o2')
    assert other.begin() is None and other.result['outcome'] == 'cold-locked'
    other.finish(0)
    assert holder.root is not None and holder.lock_fd is not None   # untouched by the loser
    holder.finish(0)
    assert Session(make_plan(tmp_path), tmp_path/'o2').begin() is not None


def _hold(path_, attempt, ready):
    s = Session(make_plan(Path(path_), attempt=attempt), Path(path_)/'held')
    s.output.mkdir(exist_ok=True)
    s.begin()
    (Path(path_)/'held'/'blob').write_bytes(b'x')
    ready.set()
    time.sleep(60)


def test_crashed_holder_releases_lock_and_root_is_wiped_not_reused(tmp_path):
    # Pre-create the cache dir with its plan so the child and parent agree.
    make_plan(tmp_path)
    ready = multiprocessing.get_context('fork').Event()
    child = multiprocessing.get_context('fork').Process(target=_hold, args=(str(tmp_path), 'dead1', ready))
    child.start()
    assert ready.wait(30)
    mid = Session(make_plan(tmp_path), tmp_path/'o')
    (tmp_path/'o').mkdir()
    assert mid.begin() is None and mid.result['outcome'] == 'cold-locked'   # holder alive
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    after, root = run_session(tmp_path, make_plan(tmp_path))
    assert after.result['outcome'] == 'wiped' and after.result['reason'] == 'unclean-previous-exit'
    assert root is not None and not (root/'blob').exists()


def test_epoch_bump_and_identity_mismatch_wipe(tmp_path):
    run_session(tmp_path, make_plan(tmp_path), grow=100)
    s, root = run_session(tmp_path, make_plan(tmp_path, epoch=1))
    assert s.result['outcome'] == 'wiped' and s.result['reason'] == 'epoch' and not (root/'blob').exists()
    (root/'blob').write_bytes(b'x')
    state = Path(tmp_path/'cache'/namespace('principal-a')/'state.json')
    data = json.loads(state.read_text())
    data['namespace'] = 'someone-else'
    state.write_text(json.dumps(data))
    s, root = run_session(tmp_path, make_plan(tmp_path, epoch=1))
    assert s.result['reason'] == 'identity-mismatch' and not (root/'blob').exists()


def test_bound_discards_over_size_and_over_growth(tmp_path):
    s, _ = run_session(tmp_path, make_plan(tmp_path, max_bytes=1024**2), grow=2*1024**2)
    assert s.result['outcome'] == 'discarded' and s.result['reason'] == 'size'
    assert not (tmp_path/'cache'/namespace('principal-a')/'slot'/'blob').exists()
    run_session(tmp_path, make_plan(tmp_path, owner='g'), grow=200_000)
    s, _ = run_session(tmp_path, make_plan(tmp_path, owner='g'), grow=1_000_000)
    assert s.result['outcome'] == 'discarded' and s.result['reason'] == 'growth'


def test_symlinked_namespace_dir_is_not_followed(tmp_path):
    p = make_plan(tmp_path)
    target = tmp_path/'elsewhere'
    target.mkdir()
    (tmp_path/'cache'/namespace('principal-a')).symlink_to(target)
    s, root = run_session(tmp_path, p)
    assert root is None and s.result['outcome'] == 'cold-unsafe' and list(target.iterdir()) == []


def test_unclean_dockerd_kill_leaves_root_dirty(tmp_path):
    p = make_plan(tmp_path)
    out = tmp_path/'o'
    out.mkdir()
    s = Session(p, out)
    s.begin()
    s.unclean = True
    s.finish(0)
    after, _ = run_session(tmp_path, make_plan(tmp_path))
    assert after.result['reason'] == 'unclean-previous-exit'


# ----------------------------------------------------------------- canary

def test_canary_is_deterministic_and_periodic():
    due = [docker_cache._canary_due(dict(canary_every=3, attempt=str(i))) for i in range(60)]
    assert due == [docker_cache._canary_due(dict(canary_every=3, attempt=str(i))) for i in range(60)]
    assert 8 <= sum(due) <= 32 and not docker_cache._canary_due(dict(canary_every=0, attempt='x'))


def pick_attempts(every):
    # warm ids end in a digit-free marker so tests can derive more of them
    canary = next(a for a in map(str, range(1000)) if docker_cache._canary_due(dict(canary_every=every, attempt=a)))
    warm = next(a for a in map(str, range(1000)) if not docker_cache._canary_due(dict(canary_every=every, attempt=a)))
    return canary, warm


def test_canary_bypasses_cache_and_catches_stale_entry(tmp_path):
    canary_id, warm_id = pick_attempts(3)
    fp = dict(inputs='lock-1', outputs={'rlibs': 'AAA'})
    cold, root = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt='first'), grow=100, fingerprint=fp)
    assert cold.result['outcome'] == 'cold-new'                      # empty root is the cold reference
    warm, root = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=warm_id), fingerprint=fp)
    assert warm.result['outcome'] == 'hit'
    # Inject a stale entry: the warm run's recorded output no longer matches what a cold run produces.
    path = tmp_path/'cache'/namespace('principal-a')/'canary.json'
    records = json.loads(path.read_text())
    records['records'][-1]['outputs'] = {'rlibs': 'STALE'}
    path.write_text(json.dumps(records))
    s, root = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=canary_id), fingerprint=fp)
    assert root is None and s.result['canary_mismatch']['this'] == 'cold'
    assert s.result['outcome'] == 'wiped'
    assert not (tmp_path/'cache'/namespace('principal-a')/'slot'/'blob').exists()
    # The next attempt starts from a fresh root.
    nxt, _ = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=warm_id+'9'))
    assert nxt.result['outcome'] == 'cold-new'


def test_canary_never_compares_verdicts_or_unnamed_inputs(tmp_path):
    """A flaky job (cold run failed, warm run passed) or a run with no fingerprint says nothing about the cache."""
    canary_id, warm_id = pick_attempts(3)
    run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt='first'), grow=100, code=1)
    run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=warm_id), code=0)
    s, _ = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=canary_id), code=1)
    assert 'canary_mismatch' not in s.result, s.result
    fp = dict(inputs='lock-1', outputs={'rlibs': 'AAA'})
    s, _ = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=warm_id+'8'), code=0, fingerprint=fp)
    canary2 = next(a for a in map(str, range(1000)) if a != canary_id and docker_cache._canary_due(dict(canary_every=3, attempt=a)))
    s, _ = run_session(tmp_path, make_plan(tmp_path, canary_every=3, attempt=canary2), code=1, fingerprint=fp)
    assert 'canary_mismatch' not in s.result, s.result       # same inputs, same outputs: verdict differences are ignored


def test_wipe_survives_a_kill_during_the_slow_delete(tmp_path):
    """The live name disappears at once; a half-deleted trash sibling is removed by the next attempt."""
    run_session(tmp_path, make_plan(tmp_path), grow=100)
    d = tmp_path/'cache'/namespace('principal-a')
    trash = d/'slot.trash-deadbeef'
    trash.mkdir()
    (trash/'half').write_bytes(b'x')
    s, root = run_session(tmp_path, make_plan(tmp_path, epoch=5))
    assert not trash.exists() and s.result['reason'] == 'epoch' and root.exists() and not (root/'blob').exists()


def test_disabled_session_is_inert(tmp_path):
    out = tmp_path/'o'
    out.mkdir()
    s = Session(None, out)
    assert s.begin() is None
    s.finish(0)
    assert list(out.iterdir()) == []


# --------------------------------------------------- worker-side plumbing

def test_worker_records_outcome_and_reports_disabled(tmp_path):
    from livestack_node.workloads.worker import WorkloadWorker
    worker = WorkloadWorker.__new__(WorkloadWorker)
    worker.docker_cache, worker.docker_cache_why = None, 'not-configured'
    completion = dict(outcome='succeeded', result={})
    worker._record_docker_cache(completion, 'rootless-docker-native', tmp_path)
    assert completion['result']['docker_cache'] == dict(outcome='disabled', reason='not-configured')
    (tmp_path/docker_cache.OUTCOME_FILE).write_text(json.dumps(dict(outcome='hit', bytes=5)))
    worker._record_docker_cache(completion, 'rootless-docker', tmp_path)
    assert completion['result']['docker_cache']['outcome'] == 'hit'
    other = dict(outcome='succeeded', result={})
    worker._record_docker_cache(other, None, tmp_path)           # native/other backends are untouched
    assert 'docker_cache' not in other['result']


def test_remove_data_never_touches_a_persistent_root(tmp_path):
    run_session(tmp_path, make_plan(tmp_path), grow=100)
    workspace = tmp_path/'attempt'
    workspace.mkdir()
    remove_data(workspace)       # no docker-data in a persistent attempt: nothing to do
    assert (tmp_path/'cache'/namespace('principal-a')/'slot'/'blob').exists()


# ----------------------------------------------- real rootless dockerd

pytestmark_docker = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd')),
    reason='requires installed rootless Docker prerequisites')

HANDLER = '''import json,subprocess,sys,time
from pathlib import Path
image,output,mode,sleep = sys.argv[1:5]
out = Path(output)
probe = {}
probe['had_marker'] = subprocess.run(['docker','image','inspect','lscache:marker'],capture_output=True).returncode == 0
build = out.parent/'ctx'; build.mkdir()
(build/'Dockerfile').write_text('FROM '+image+'\\nARG N\\nRUN --mount=type=cache,id=lscache,target=/c echo $N >/dev/null; n=$(cat /c/n 2>/dev/null || echo 0); echo $((n+1)) > /c/n; echo COUNT=$((n+1)) > /count\\n')
done = subprocess.run(['docker','build','--build-arg','N='+str(time.time()),'-t','lscache:marker',str(build)],capture_output=True,text=True)
probe['build_rc'] = done.returncode
ran = subprocess.run(['docker','run','--rm','lscache:marker','cat','/count'],capture_output=True,text=True)
probe['count'] = ran.stdout.strip()
probe['run_err'] = ran.stderr[-300:]
probe['build_err'] = done.stderr[-300:]
probe['info_root'] = subprocess.run(['docker','info','--format','{{.DockerRootDir}}'],capture_output=True,text=True).stdout.strip()
(out/'probe.json').write_text(json.dumps(probe))
(out/'docker-cache-fingerprint.json').write_text(json.dumps({'inputs':'x','outputs':{'count-kind':'n'}}))
time.sleep(float(sleep))
raise SystemExit(0 if mode != 'fail' else 3)
'''


def attempt(tmp_path, name, p, *, sleep=0, native=False, mode='ok', wait=True):
    work = tmp_path/name
    work.mkdir()
    output = work/'output'
    output.mkdir()
    script = work/'handler.py'
    script.write_text(HANDLER)
    lease = work/'lease'
    lease.write_text(str(time.monotonic()+600))
    executor = SystemdExecutor('lscache-'+uuid.uuid4().hex)
    attempt_id = uuid.uuid4().hex
    kwargs = dict(rootless_docker=True)
    if native:
        kwargs.update(rootless_native=True, native_host_address=subprocess.check_output(['hostname', '-I'], text=True).split()[0])
    if p is not None:
        kwargs['docker_cache'] = dict(p, attempt=attempt_id)
    executor.start(attempt_id, [sys.executable, str(script), IMAGE, str(output), mode, str(sleep)], work, output,
                   env=dict(os.environ), cpu=1, memory_bytes=1024**3, lease_file=lease, max_seconds=400, **kwargs)
    handle = (executor, attempt_id, work, output)
    return finish(handle) if wait else handle


def finish(handle):
    executor, attempt_id, work, output = handle
    deadline = time.monotonic()+300
    while time.monotonic() < deadline:
        result = executor.exit_result(output)
        if result is not None:
            break
        time.sleep(.3)
    else:
        raise AssertionError('attempt did not finish')
    executor.stop(attempt_id)
    remove_data(work)
    probe = json.loads((output/'probe.json').read_text()) if (output/'probe.json').exists() else {}
    return probe, docker_cache.read_outcome(output), result, work


@pytestmark_docker
def test_real_dockerd_hit_namespaces_and_disabled_baseline(tmp_path):
    p = make_plan(tmp_path, canary_every=0)
    probe1, out1, res1, work1 = attempt(tmp_path, 'a1', p)
    assert res1['exit_code'] == 0 and out1['outcome'] == 'cold-new', (out1, probe1)
    assert probe1['count'] == 'COUNT=1' and not probe1['had_marker'] and probe1['info_root'] == '/run/harmony/data'
    assert not (work1/'docker-data').exists()                     # the persistent root replaced it
    probe2, out2, res2, _ = attempt(tmp_path, 'a2', p)
    assert out2['outcome'] == 'hit' and out2['bytes'] > 0, out2
    assert probe2['had_marker'] and probe2['count'] == 'COUNT=2', probe2   # image AND cache mount survived
    # Another principal on the same path sees none of it.
    probe3, out3, _, _ = attempt(tmp_path, 'b1', make_plan(tmp_path, owner='principal-b'))
    assert out3['outcome'] == 'cold-new' and not probe3['had_marker'] and probe3['count'] == 'COUNT=1'
    # Disabled is today's behaviour: private docker-data, no record, nothing persisted.
    probe4, out4, res4, _ = attempt(tmp_path, 'off', None)
    assert out4 is None and not probe4['had_marker'] and probe4['count'] == 'COUNT=1'
    assert out2['prune'].get('builder') == 'ok', out2['prune']


@pytestmark_docker
def test_real_dockerd_concurrent_attempts_never_share_a_root(tmp_path):
    p = make_plan(tmp_path)
    first = attempt(tmp_path, 'c1', p, sleep=25, wait=False)
    time.sleep(8)
    second = attempt(tmp_path, 'c2', p, wait=False)
    probe2, out2, _, _ = finish(second)
    probe1, out1, _, _ = finish(first)
    assert out1['outcome'] == 'cold-new' and out2['outcome'] == 'cold-locked', (out1, out2)
    assert probe2['count'] == 'COUNT=1' and probe1['count'] == 'COUNT=1', (probe1, probe2)


@pytestmark_docker
def test_real_native_frontend_prunes_while_dockerd_is_up_and_hits(tmp_path):
    p = make_plan(tmp_path)
    probe1, out1, res1, _ = attempt(tmp_path, 'n1', p, native=True)
    assert res1['exit_code'] == 0 and out1['outcome'] == 'cold-new', (out1, probe1)
    assert set(out1['prune']) >= {'volume', 'network', 'image', 'builder', 'docker_bytes'}, out1
    probe2, out2, _, _ = attempt(tmp_path, 'n2', p, native=True)
    assert out2['outcome'] == 'hit' and probe2['had_marker'] and probe2['count'] == 'COUNT=2', (out2, probe2)


@pytestmark_docker
def test_real_dockerd_that_cannot_start_on_the_root_is_wiped_and_attempt_runs_cold(tmp_path):
    p = make_plan(tmp_path)
    attempt(tmp_path, 'w1', p)
    slot = tmp_path/'cache'/namespace('principal-a')/'slot'
    # Corrupt the store while leaving the clean marker intact, as a bad disk or a hand edit would.
    code, detail = run_in_userns(['/bin/sh', '-c', 'rm -rf %s/image %s/overlay2/l && echo junk > %s/overlay2/l && echo junk > %s/image'
                                  % (slot, slot, slot, slot)])
    assert code == 0, detail
    probe, out, res, _ = attempt(tmp_path, 'w2', p)
    assert res['exit_code'] == 0 and probe['count'] == 'COUNT=1', (out, probe)
    assert out['outcome'] == 'wiped' and out['reason'] == 'dockerd-start-failed', out


def test_unreadable_subtree_is_an_error_not_zero(tmp_path):
    tree = tmp_path/'t'
    (tree/'sealed').mkdir(parents=True)
    (tree/'sealed'/'f').write_bytes(b'x'*8192)
    (tree/'sealed').chmod(0)
    try:
        nbytes, errors = docker_cache.tree_bytes(tree)
        assert errors == 1
    finally:
        for d in tmp_path.rglob('sealed'):
            d.chmod(0o700)


def test_native_frontend_is_told_how_long_the_bookkeeping_may_take(tmp_path):
    out = tmp_path/'o'
    out.mkdir()
    s = Session(make_plan(tmp_path), out)
    s.begin()
    s.announce()
    assert json.loads((out/docker_cache.SESSION_FILE).read_text())['finish_wait'] == docker_cache.FINISH_WAIT > 60
    s.finish(0)
    off = tmp_path/'p'
    off.mkdir()
    Session(None, off).announce()
    assert list(off.iterdir()) == []
