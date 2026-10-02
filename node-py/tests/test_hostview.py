"""hostview against real files in a temp /proc + cgroup tree (no mocks of the readers)."""
import json
import os

from livestack_node.hostview import HostView, cgroup_memory, meminfo, psi

GIB = 1024**3


def proc_tree(root, *, available_kib=20*1024**2, swap_in=100, pressure=True):
    (root/'pressure').mkdir(parents=True, exist_ok=True)
    (root/'meminfo').write_text(f'MemTotal:       32505856 kB\nMemFree:  1 kB\nMemAvailable:   {available_kib} kB\n')
    (root/'vmstat').write_text(f'nr_free_pages 1\npswpin {swap_in}\npswpout 9\n')
    if pressure:
        (root/'pressure'/'memory').write_text(
            'some avg10=0.16 avg60=0.76 avg300=0.74 total=1\nfull avg10=0.10 avg60=6.50 avg300=0.66 total=1\n')
        (root/'pressure'/'io').write_text(
            'some avg10=5.35 avg60=8.64 avg300=8.92 total=1\nfull avg10=4.48 avg60=6.83 avg300=6.31 total=1\n')
        (root/'pressure'/'cpu').write_text('some avg10=1.00 avg60=2.00 avg300=3.00 total=1\n')
    return root


def cgroup(path, current, peak=None):
    path.mkdir(parents=True, exist_ok=True)
    (path/'memory.current').write_text(f'{current}\n')
    if peak is not None:
        (path/'memory.peak').write_text(f'{peak}\n')


def test_readers_take_real_proc_files(tmp_path):
    proc = proc_tree(tmp_path/'proc')
    assert meminfo(proc) == {'total': 32505856*1024, 'available': 20*GIB}
    assert psi('memory', proc) == {'some_avg10': .16, 'some_avg60': .76, 'full_avg10': .10, 'full_avg60': 6.5}
    # Older kernels' cpu file has no `full` line: unknown, not zero.
    assert psi('cpu', proc)['full_avg60'] is None
    assert psi('memory', proc_tree(tmp_path/'nopsi', pressure=False)) is None
    assert cgroup_memory(tmp_path/'absent') is None


def test_sample_reports_every_harmony_tenant_on_the_host(tmp_path):
    proc = proc_tree(tmp_path/'proc')
    root = tmp_path/'cgroup'
    app = root/'user.slice/user-1000.slice/user@1000.service/app.slice'
    mine, sibling = 'a'*32, 'b'*32
    cgroup(app/f'harmony-work-{"1"*16}-{mine}.service', 3*GIB, 4*GIB)
    cgroup(app/f'harmony-work-{"2"*16}-{sibling}.service', 2*GIB, 9*GIB)
    cgroup(app/'some-other.service', 7*GIB, 7*GIB)
    cgroup(root/'system.slice/harmony-klein-0.service', int(3.6*GIB), 16*GIB)
    view = HostView(services=['system.slice/harmony-klein-0.service', 'system.slice/stopped.service'],
                    peaks_path=tmp_path/'peaks.json', attempts_dir=app, reserve_bytes=GIB,
                    proc=proc, cgroup_root=root)
    sample = view.sample()
    assert sample['memory_available_bytes'] == 20*GIB and sample['memory_reserve_bytes'] == GIB
    # The sibling identity's attempt is reported too; unrelated units are not.
    assert sample['attempts'] == {mine: 3*GIB, sibling: 2*GIB}
    assert sample['services'] == {
        'system.slice/harmony-klein-0.service': {'current_bytes': int(3.6*GIB), 'peak_bytes': 16*GIB},
        # Stopped: holds nothing now and has never been seen; it may start and load.
        'system.slice/stopped.service': {'current_bytes': 0, 'peak_bytes': 0}}
    assert sample['psi']['memory']['full_avg60'] == 6.5
    assert sample['swap_in_bytes_per_second'] is None, 'one sample has no rate: unknown, not zero'


def test_learned_service_peak_survives_a_restart(tmp_path):
    """A restart resets memory.peak; on 2026-10-01 klein-0 read 16 GB at its
    restart and idled at ~3.6 GB after it. The learned peak must not forget."""
    proc = proc_tree(tmp_path/'proc')
    root = tmp_path/'cgroup'
    unit = root/'system.slice/harmony-klein-0.service'
    cgroup(unit, int(3.6*GIB), 16*GIB)
    kwargs = dict(services=['system.slice/harmony-klein-0.service'], peaks_path=tmp_path/'peaks.json',
                  attempts_dir=tmp_path/'none', proc=proc, cgroup_root=root)
    HostView(**kwargs).sample()
    cgroup(unit, GIB, GIB)  # restarted
    sample = HostView(**kwargs).sample()
    assert sample['services']['system.slice/harmony-klein-0.service'] == {'current_bytes': GIB, 'peak_bytes': 16*GIB}
    assert json.loads((tmp_path/'peaks.json').read_text()) == {'system.slice/harmony-klein-0.service': 16*GIB}


def test_swap_in_rate_comes_from_two_samples(tmp_path):
    proc = proc_tree(tmp_path/'proc', swap_in=100)
    clock = [10.0]
    view = HostView(attempts_dir=tmp_path/'none', proc=proc, cgroup_root=tmp_path, clock=lambda: clock[0])
    assert view.sample()['swap_in_bytes_per_second'] is None
    proc_tree(proc, swap_in=100+8192)
    clock[0] = 12.0
    assert view.sample()['swap_in_bytes_per_second'] == 4096*os.sysconf('SC_PAGE_SIZE')


def test_invalid_service_paths_are_refused(tmp_path):
    for bad in (['/sys/fs/cgroup/x'], ['a/../b'], ['']):
        try:
            HostView(services=bad)
        except ValueError:
            continue
        raise AssertionError(bad)
