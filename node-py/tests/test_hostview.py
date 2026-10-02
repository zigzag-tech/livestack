"""hostview against real files in a temp /proc + cgroup tree (no mocks of the readers)."""
import json
import os

from livestack_node.hostview import HostView, cgroup_nonreclaimable, meminfo, psi

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


def cgroup(path, anon, cache=0, shmem=0, kernel=0, slab_reclaimable=0):
    """A cgroup v2 dir as the kernel lays it out: memory.current counts page
    cache; memory.stat splits it out."""
    path.mkdir(parents=True, exist_ok=True)
    (path/'memory.current').write_text(f'{anon+cache+shmem+kernel}\n')
    (path/'memory.peak').write_text(f'{anon+cache+shmem+kernel}\n')
    (path/'memory.stat').write_text(
        f'anon {anon}\nfile {cache+shmem}\nkernel {kernel}\nshmem {shmem}\n'
        f'slab_reclaimable {slab_reclaimable}\nslab {slab_reclaimable}\n')


def test_readers_take_real_proc_files(tmp_path):
    proc = proc_tree(tmp_path/'proc')
    assert meminfo(proc) == {'total': 32505856*1024, 'available': 20*GIB}
    assert psi('memory', proc) == {'some_avg10': .16, 'some_avg60': .76, 'full_avg10': .10, 'full_avg60': 6.5}
    # Older kernels' cpu file has no `full` line: unknown, not zero.
    assert psi('cpu', proc)['full_avg60'] is None
    assert psi('memory', proc_tree(tmp_path/'nopsi', pressure=False)) is None
    assert cgroup_nonreclaimable(tmp_path/'absent') is None


def test_sample_reports_every_harmony_tenant_on_the_host(tmp_path):
    proc = proc_tree(tmp_path/'proc')
    root = tmp_path/'cgroup'
    app = root/'user.slice/user-1000.slice/user@1000.service/app.slice'
    mine, sibling = 'a'*32, 'b'*32
    cgroup(app/f'harmony-work-{"1"*16}-{mine}.service', 3*GIB, cache=4*GIB)
    cgroup(app/f'harmony-work-{"2"*16}-{sibling}.service', 2*GIB, shmem=GIB)
    cgroup(app/'some-other.service', 7*GIB)
    # klein-0 just after a load, as measured 2026-10-02 02:52 UTC: 17.8 GB
    # memory.current, 1.36 GB anon, the rest the safetensors in page cache.
    cgroup(root/'system.slice/harmony-klein-0.service', int(1.36*GIB), cache=int(16.4*GIB),
           kernel=GIB, slab_reclaimable=int(.75*GIB))
    view = HostView(services=['system.slice/harmony-klein-0.service', 'system.slice/stopped.service'],
                    peaks_path=tmp_path/'peaks.json', attempts_dir=app, reserve_bytes=GIB,
                    proc=proc, cgroup_root=root)
    sample = view.sample()
    assert sample['memory_available_bytes'] == 20*GIB and sample['memory_reserve_bytes'] == GIB
    # Non-reclaimable only (page cache is already inside MemAvailable). The
    # sibling identity's attempt is reported too; unrelated units are not.
    assert sample['attempts'] == {mine: 3*GIB, sibling: 3*GIB}
    klein = int(1.36*GIB) + GIB - int(.75*GIB)
    assert sample['services'] == {
        'system.slice/harmony-klein-0.service': {'current_bytes': klein, 'peak_bytes': klein},
        # Stopped: holds nothing now and has never been seen; it may start and load.
        'system.slice/stopped.service': {'current_bytes': 0, 'peak_bytes': 0}}
    assert sample['psi']['memory']['full_avg60'] == 6.5
    assert sample['swap_in_bytes_per_second'] is None, 'one sample has no rate: unknown, not zero'


def test_learned_service_peak_is_the_max_sample_and_survives_a_restart(tmp_path):
    proc = proc_tree(tmp_path/'proc')
    root = tmp_path/'cgroup'
    unit = root/'system.slice/polytts.service'
    kwargs = dict(services=['system.slice/polytts.service'], peaks_path=tmp_path/'peaks.json',
                  attempts_dir=tmp_path/'none', proc=proc, cgroup_root=root)
    cgroup(unit, 5*GIB, cache=8*GIB)
    HostView(**kwargs).sample()
    cgroup(unit, GIB, cache=12*GIB)  # restarted; cache-heavy reload, little anon
    sample = HostView(**kwargs).sample()
    assert sample['services']['system.slice/polytts.service'] == {'current_bytes': GIB, 'peak_bytes': 5*GIB}
    assert json.loads((tmp_path/'peaks.json').read_text()) == {'system.slice/polytts.service': 5*GIB}


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


def residence_server(payloads):
    """A real local HTTP server answering /livestack/residence per port path."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payloads[self.path.split('/')[1]]).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_service_spike_is_outstanding_only_while_a_unit_is_not_resident(tmp_path):
    """A load spike can only recur while a unit is not loaded. A resident server
    reports resident=True (not charged); a non-resident one False; one that
    cannot be read None, which placement charges like False."""
    import socket
    proc = proc_tree(tmp_path/'proc')
    root = tmp_path/'cgroup'
    for name in ('klein', 'polytts', 'gone', 'stopped'):
        if name != 'stopped':
            cgroup(root/f'system.slice/{name}.service', GIB, cache=15*GIB)
    server = residence_server({
        'klein': {'units': [{'kind': 'flux', 'resident': True}]},
        'polytts': {'units': [{'kind': 'qwen', 'resident': True}, {'kind': 'voxcpm', 'resident': False}]},
    })
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        dead = s.getsockname()[1]  # closed again before use: nothing listens there
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        view = HostView(services=[
            {'path': 'system.slice/klein.service', 'residence': f'{base}/klein/livestack/residence'},
            {'path': 'system.slice/polytts.service', 'residence': f'{base}/polytts/livestack/residence'},
            {'path': 'system.slice/gone.service', 'residence': f'http://127.0.0.1:{dead}/livestack/residence'},
            {'path': 'system.slice/stopped.service', 'residence': f'{base}/klein/livestack/residence'},
            'system.slice/plain.service',
        ], attempts_dir=tmp_path/'none', proc=proc, cgroup_root=root)
        services = view.sample()['services']
    finally:
        server.shutdown()
    assert services['system.slice/klein.service']['resident'] is True
    assert services['system.slice/polytts.service']['resident'] is False
    assert services['system.slice/gone.service']['resident'] is None, 'unreachable is unknown, not resident'
    assert services['system.slice/stopped.service']['resident'] is False, 'no cgroup: nothing is loaded'
    assert 'resident' not in services['system.slice/plain.service'], 'plain entries behave as before'
