"""Single model store guard and scratch sweep (livestack_node/workloads/store_guard.py)."""
import logging
import os
import time

import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store_guard import StoreGuard

REPO = 'models--black-forest-labs--FLUX.2-dev'


def make(tmp_path, **raw):
    store, ws, state = tmp_path/'huggingface', tmp_path/'workspace', tmp_path/'state'
    for p in (store/'hub', ws, state):
        p.mkdir(parents=True)
    (store/'hub'/REPO/'snapshots').mkdir(parents=True)
    (store/'hub'/REPO/'snapshots'/'w.bin').write_bytes(b'x'*4096)
    return StoreGuard(dict(model_store=str(store), max_repo_bytes=1024**2, **raw), workspace=ws, state_dir=state), store, ws


def blob(path, size=4096):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'y'*size)


def test_duplicate_repo_in_workspace_trips_guard_loudly(tmp_path, caplog):
    guard, store, ws = make(tmp_path)
    blob(ws/'e2e'/'hf-cache'/'hub'/REPO/'snapshots'/'w.bin')
    with caplog.at_level(logging.ERROR):
        found = guard.scan()
    assert [(v['kind'], v['repo']) for v in found] == [('duplicate_of_host_store', REPO)]
    assert 'single_store_violation' in caplog.text
    assert guard.state_path.exists()
    assert (ws/'e2e'/'hf-cache'/'hub'/REPO).exists(), 'a violation is reported, never deleted'


def test_oversize_unknown_repo_is_over_bound(tmp_path):
    guard, store, ws = make(tmp_path)
    blob(ws/'a'/'models--org--big'/'x.bin', 2*1024**2)
    assert [v['kind'] for v in guard.scan()] == ['over_bound']


def test_small_non_duplicate_and_host_store_are_clean(tmp_path):
    guard, store, ws = make(tmp_path)
    blob(ws/'a'/'models--org--tiny'/'x.bin', 10)
    assert guard.scan() == []
    assert guard.store.exists() and (store/'hub'/REPO).exists()


def test_host_store_inside_workspace_refused(tmp_path):
    ws = tmp_path/'workspace'
    (ws/'hf').mkdir(parents=True)
    with pytest.raises(WorkloadError):
        StoreGuard(dict(model_store=str(ws/'hf')), workspace=ws, state_dir=tmp_path)


def test_job_env_points_at_host_store_offline(tmp_path):
    guard, store, ws = make(tmp_path)
    env = guard.job_env()
    assert env == dict(HF_HOME=str(store), HF_HUB_CACHE=str(store/'hub'), HF_HUB_OFFLINE='1')


def test_environment_with_workspace_cache_fails_startup(tmp_path):
    guard, store, ws = make(tmp_path)
    guard.check_environment({'HF_HOME': str(store)})
    with pytest.raises(WorkloadError, match='single_store_violation'):
        guard.check_environment({'HF_HOME': str(ws/'hf-cache')})


def test_sweep_removes_only_stale_incomplete_and_over_cap_scratch(tmp_path):
    guard, store, ws = make(tmp_path, sweep=dict(scratch_cap_bytes=6000))
    old = time.time()-3*86400
    stale, fresh = ws/'a'/'x.incomplete', ws/'a'/'y.incomplete'
    blob(stale); blob(fresh)
    os.utime(stale, (old, old))
    host_incomplete = store/'hub'/REPO/'blobs'/'z.incomplete'
    blob(host_incomplete); os.utime(host_incomplete, (old, old))
    blob(ws/'flux2-offload-old'/'t.bin'); os.utime(ws/'flux2-offload-old', (old, old))
    blob(ws/'flux2-offload-n1'/'t.bin'); blob(ws/'flux2-offload-n2'/'t.bin')
    os.utime(ws/'flux2-offload-n1', (time.time()-20, time.time()-20))
    blob(ws/'keep'/'data.bin')
    removed = guard.sweep()
    assert not stale.exists() and fresh.exists()
    assert not (ws/'flux2-offload-old').exists()
    assert (ws/'flux2-offload-n2').exists() and not (ws/'flux2-offload-n1').exists()  # cap 6000: newest kept
    assert (ws/'keep'/'data.bin').exists()
    assert host_incomplete.exists(), 'the host store is never swept'
    assert removed['incomplete'] == 1 and removed['scratch'] == 2


def test_unknown_config_key_rejected(tmp_path):
    with pytest.raises(WorkloadError):
        StoreGuard(dict(model_store=str(tmp_path/'h'), bogus=1), workspace=tmp_path/'w', state_dir=tmp_path)


def test_worker_wires_guard_env_and_rejects_workspace_cache(tmp_path):
    import sys
    from livestack_node.workloads.worker import WorkloadWorker
    store = tmp_path/'huggingface'
    (store/'hub').mkdir(parents=True)
    cfg = dict(authority='http://127.0.0.1:1', token='w'*32, worker='w1', state_dir=str(tmp_path/'state'),
               workspace=str(tmp_path/'workspace'), require_dedicated_filesystem=False,
               handler_runtimes={'python3': sys.executable},
               capacity={'cpu': 4, 'memory_bytes': 128*1024**2, 'disk_bytes': 192*1024**3},
               memory_reserve_bytes=0, environment={'PATH': '/usr/bin:/bin'},
               handlers={'native.v1': dict(argv=[sys.executable, '-c', 'pass'], outputs=[])},
               store_guard=dict(model_store=str(store)))
    w = WorkloadWorker(cfg)
    assignment = dict(owner='o', spec=dict(input_digest='sha256:'+'0'*64, labels={}))
    root = tmp_path/'workspace'/'a1'
    env = w._attempt_env(assignment, root, root/'output', root/'objects', 'a1')
    assert env['HF_HOME'] == str(store) and env['HF_HUB_OFFLINE'] == '1'
    bad = dict(cfg, environment={'HF_HOME': str(tmp_path/'workspace'/'hf-cache')}, state_dir=str(tmp_path/'s2'))
    with pytest.raises(WorkloadError, match='single_store_violation'):
        WorkloadWorker(bad)
