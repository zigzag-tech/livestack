"""Worker disk honesty, named placement refusals and the verified CPU signal
(openspec/changes/storage-headroom-admission, sections 3 and 4)."""
import json
import os
import sys
from contextlib import contextmanager

import pytest

from livestack_node.workloads import cpu_admission
from livestack_node.workloads.cpu_admission import CpuAdmission, RunqueueSampler, psi_signal, selftest
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.worker import WorkloadWorker

GIB = 1024**3
PSI = ('some avg10={some} avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')


def config(tmp_path, **extra):
    base = dict(authority='http://127.0.0.1:1', token='w'*32, worker='w1', state_dir=str(tmp_path/'state'),
                workspace=str(tmp_path/'workspace'), require_dedicated_filesystem=False,
                handler_runtimes={'python3': sys.executable},
                capacity={'cpu': 4, 'memory_bytes': 128*1024**2, 'disk_bytes': 192*GIB},
                memory_reserve_bytes=0, environment={'PATH': '/usr/bin:/bin'},
                handlers={'native.v1': dict(argv=[sys.executable, '-c', 'pass'], outputs=[])})
    base.update(extra)
    return base


def worker(tmp_path, free, **extra):
    w = WorkloadWorker(config(tmp_path, **extra))
    w._disk = lambda path: (192*GIB, free)
    return w


# ---- disk ---------------------------------------------------------------------

def test_reserve_over_free_reports_disk_unavailable_and_clears(tmp_path):
    w = worker(tmp_path, 60*GIB, disk_reserve_bytes=64*GIB)
    report = w.report()
    assert report['available']['disk_bytes'] == 0
    assert report['disk_unavailable'] == dict(filesystem=str(w.workspace), free_bytes=60*GIB,
                                              reserve_bytes=64*GIB, offered_bytes=0,
                                              capacity_bytes=192*GIB, reason='reserve_exceeds_free')
    w.config['disk_reserve_bytes'] = 10*GIB       # the operator shrinks the reserve
    report = w.report()
    assert report['available']['disk_bytes'] == 50*GIB and 'disk_unavailable' not in report


def test_a_normal_reserve_reports_nothing(tmp_path):
    assert 'disk_unavailable' not in worker(tmp_path, 100*GIB, disk_reserve_bytes=GIB).report()


def test_authority_stores_the_report_and_placement_names_the_disk(tmp_path, caplog):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'}, clock=lambda: now[0])
    w = worker(tmp_path, 60*GIB, disk_reserve_bytes=64*GIB)
    store.register('w1', 'h1', 'boot', w.report())
    caller_spec = dict(version=1, key='k', handler='native.v1', input_digest='0'*64,
                       need={'cpu': 1, 'disk_bytes': 8*GIB})
    job = store.submit('alice', caller_spec)
    assert store.claim('w1', 'boot') is None            # nothing placeable: the disk is withheld
    text = store.get('alice', job['id'])['reason']
    now[0] += 301                                       # same refusal past stall_report_seconds
    store.register('w1', 'h1', 'boot', w.report())
    store.claim('w1', 'boot')
    assert any('placement_stalled' in r.getMessage() and 'disk_reserve' in r.getMessage()
               for r in caplog.records)
    assert 'disk offered 0.0 of 192.0 GiB (free 60.0 GiB < reserve 64.0 GiB' in text
    assert 'disk_reserve' in text and 'insufficient shared host resources' not in text


def test_invalid_disk_report_is_refused(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'})
    report = worker(tmp_path, 60*GIB, disk_reserve_bytes=64*GIB).report()
    report['disk_unavailable']['free_bytes'] = -1
    with pytest.raises(WorkloadError, match='disk_unavailable'):
        store.register('w1', 'h1', 'boot', report)


# ---- cpu ----------------------------------------------------------------------

def test_policy_validation_fails_closed():
    for bad in [dict(policy='full'), dict(selftest_seconds=99), dict(fallback='loadavg'), dict(nope=1),
                dict(stall_some_avg10_percent=101)]:
        with pytest.raises(ValueError):
            CpuAdmission.validate(bad)
    assert CpuAdmission.validate(dict(policy='psi_some')).fallback == 'runqueue'


@contextmanager
def fake_burn(path, value):
    """Burn seam: while running, the fake pressure file reads `value`."""
    def factory(count):
        @contextmanager
        def ctx():
            path.write_text(PSI.format(some=value))
            yield
        return ctx()
    yield factory


def test_selftest_active_when_signal_moves_and_inert_when_frozen(tmp_path):
    path = tmp_path/'cpu'
    path.write_text(PSI.format(some='0.00'))
    read = psi_signal(str(path), 'some', 'avg10')
    with fake_burn(path, '30.00') as burn:
        moved = selftest(read, policy='psi_some', threshold=40, cores=2, seconds=0.3, burn=burn, poll=.05)
    assert moved['state'] == 'active'
    path.write_text(PSI.format(some='0.00'))
    frozen = selftest(read, policy='psi_some', threshold=40, cores=2, seconds=0.3,
                      burn=lambda n: contextmanager(lambda: (yield))(), poll=.05)
    assert frozen['state'] == 'inert' and 'cannot gate admission' in frozen['detail']


def test_selftest_unreadable_is_inert_and_busy_host_is_unverified(tmp_path):
    assert selftest(psi_signal(str(tmp_path/'missing'), 'some', 'avg10'), policy='psi_some', threshold=40,
                    cores=1, seconds=.1)['state'] == 'inert'
    path = tmp_path/'cpu'
    path.write_text(PSI.format(some='55.00'))
    assert selftest(psi_signal(str(path), 'some', 'avg10'), policy='psi_some', threshold=40, cores=1,
                    seconds=.1)['state'] == 'active_unverified'


def test_frozen_pressure_file_makes_worker_report_no_cpu_and_no_fallback(tmp_path):
    path = tmp_path/'cpu'
    path.write_text(PSI.format(some='0.00'))
    w = worker(tmp_path, 100*GIB, disk_reserve_bytes=GIB,
               cpu_admission=dict(policy='psi_some', psi_path=str(path), selftest_seconds=1))
    assert w.cpu_signal['state'] == 'inert'
    report = w.report()
    assert report['available']['cpu'] == 0 and report['cpu_signal']['policy'] == 'psi_some'
    assert report['cpu_signal']['state'] == 'inert'


def test_missing_pressure_file_uses_named_runqueue_fallback_and_idle_host_offers_cpu(tmp_path):
    stat = tmp_path/'stat'
    stat.write_text('cpu 1 1 1 1\nprocs_running 1\n')
    w = worker(tmp_path, 100*GIB, disk_reserve_bytes=GIB,
               cpu_admission=dict(policy='psi_some', psi_path=str(tmp_path/'none'), proc_root=str(tmp_path),
                                  selftest_seconds=1))
    # the fake /proc/stat never moves under the real burn: the signal is inert, said by name
    assert w.cpu_signal['state'] == 'inert' and 'runqueue' in w.cpu_signal['detail']
    assert w.report()['available']['cpu'] == 0


def test_real_burn_moves_runqueue_and_psi_some_but_not_full(tmp_path):
    cores = os.cpu_count() or 1
    sampler = RunqueueSampler('/proc', 3, interval=.25).start()
    try:
        result = selftest(sampler, policy='runqueue', threshold=1000, cores=cores, seconds=2, poll=.25)
    finally:
        sampler.stop()
    assert result['state'] == 'active', result
    if not os.path.exists('/proc/pressure/cpu'):
        pytest.skip('kernel exposes no /proc/pressure/cpu')
    some = selftest(psi_signal('/proc/pressure/cpu', 'some', 'avg10'), policy='psi_some', threshold=1000,
                    cores=cores, seconds=4, poll=.5)
    assert some['state'] == 'active', some


def test_load_average_above_cores_does_not_withhold_a_verified_policy(tmp_path):
    stat = tmp_path/'stat'
    stat.write_text('procs_running 1\n')
    sampler = RunqueueSampler(str(tmp_path), 2, cores=4, interval=1).start()
    w = worker(tmp_path, 100*GIB, disk_reserve_bytes=GIB, cpu_admission=dict(policy='runqueue', selftest_seconds=1))
    w._cpu_read, w._cpu_threshold = sampler, 2.0   # a verified runqueue signal reading idle
    assert os.getloadavg()[0] >= 0
    assert w._available_cpu(dict(cpu=4)) == 4
    stat.write_text('procs_running 41\n')
    sampler.sample()
    assert w._available_cpu(dict(cpu=4)) == 0
    sampler.stop()
