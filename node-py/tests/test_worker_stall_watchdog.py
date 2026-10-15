"""A worker whose main thread wedges fails closed (2026-10-15, xc-win-1-wsl-2: it
claimed e2e.full, then went silent for hours with its authority link half-closed).
The wedge here is a local call with no timeout; the watchdog must name the phase
and stop the process, and the idle path must not trip it."""
import logging
import socket
import threading
import time

from livestack_node import transport
from livestack_node.workloads import worker as worker_module
from livestack_node.workloads.stall_watchdog import StallWatchdog
from livestack_node.workloads.worker import WorkloadWorker

from test_workload_worker import fleet, submit  # noqa: F401  (fixture reuse)


def test_watchdog_fires_once_naming_phase_and_stacks(caplog):
    fired = []
    dog = StallWatchdog(poll=.02, on_stall=lambda phase, waited: fired.append(phase)).start()
    with caplog.at_level(logging.INFO):
        dog.beat('transfer', .1)
        deadline = time.monotonic()+5
        while not fired and time.monotonic() < deadline:
            time.sleep(.02)
    dog.stop()
    assert fired == ['transfer']
    text = caplog.text
    assert "stalled in phase 'transfer'" in text and '--- thread' in text


def test_beating_main_thread_is_never_stalled():
    fired = []
    dog = StallWatchdog(poll=.02, on_stall=lambda *a: fired.append(a)).start()
    for _ in range(15):
        dog.beat('step', .2)
        time.sleep(.02)
    dog.stop()
    assert fired == []


def test_worker_wedged_after_claim_is_stopped_with_its_phase(fleet, monkeypatch):  # noqa: F811
    store, config, caller, digest = fleet
    submit(caller, digest)
    release, fired = threading.Event(), []
    monkeypatch.setattr(worker_module, 'STALL_TRANSFER_SECONDS', .2)
    worker = WorkloadWorker(config)
    worker.watchdog = StallWatchdog(poll=.02, on_stall=lambda phase, waited: (fired.append(phase), release.set())).start()
    # The wedge: a transfer that never returns and never times out.
    worker.transfer.get = lambda *a, **k: release.wait(30) and (_ for _ in ()).throw(
        RuntimeError('released by the watchdog test'))
    try:
        worker.step()
    finally:
        worker.watchdog.stop()
        worker.close()
    assert fired == ['transfer']


def test_kept_connection_probes_idle_peers_with_tcp_keepalive():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    kept = transport.KeptConnection('http://127.0.0.1:%d' % server.getsockname()[1], timeout=.3)
    try:
        # A peer that accepts and never answers: the call fails within the deadline, not never.
        threading.Thread(target=server.accept, daemon=True).start()
        started = time.monotonic()
        try:
            kept.request('GET', '/x')
            assert False, 'a silent peer must fail the call'
        except transport.URLError:
            pass
        assert time.monotonic()-started < 3
        probe = socket.socket()
        transport._keepalive(probe)
        assert probe.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, 'TCP_KEEPIDLE'):
            assert probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
        probe.close()
    finally:
        kept.close()
        server.close()
