"""A caller that hangs up stops waiting on hostd, through a real uvicorn server.

xc-tower-ubuntu, 2026-11-03: histo's chain was killed mid-request while its
image request waited for both GPUs. Nothing upstream noticed, so the request
kept its place in line, took both leases at 12:22 and ran its job for nobody,
while the chain's retry waited behind it. A caller that is gone must give up
its place, fail a drain it began, and never be left holding a lease.
"""
import asyncio
import json
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

from livestack_node import hostd  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402
from livestack_node.planner import Placement  # noqa: E402

from test_exclusive_http import _Peer, _broker, _exclusive_body  # noqa: E402


class _BusyPeer(_Peer):
    """A tenant finishing work: an exclusive drain waits for it."""

    def placements(self):
        return [Placement(self.unit.kind, self.device_id, loaded_at=0.0, busy=True)]


@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")
    monkeypatch.setattr(hostd, "ADMIT_WAIT_POLL_S", 0.05)
    servers = []

    def start(broker):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(build_app(broker), host="127.0.0.1", port=port,
                                               log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        while not server.started:
            time.sleep(0.02)
        return port

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


def _post(port, body):
    """Send POST /admit and return the open socket (the caller, still waiting)."""
    data = json.dumps(body).encode()
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"POST /admit HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                 + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    return sock


def _admit(port, body):
    import httpx
    return httpx.post(f"http://127.0.0.1:{port}/admit", json=body, timeout=30).json()


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_queued_exclusive_caller_that_hangs_up_leaves_the_line(serve):
    broker = _broker([_Peer("http://alpha", "alpha", resident=False)], time.monotonic)
    port = serve(broker)
    holder = _admit(port, _exclusive_body("holder"))
    assert holder["granted"] is True

    caller = _post(port, {**_exclusive_body("abandoned", owner="bob"), "wait_s": 60})
    assert _until(lambda: "abandoned" in broker._exclusive_waiters.get("gpu0", []))
    caller.close()
    assert _until(lambda: "abandoned" not in broker._exclusive_waiters.get("gpu0", []))

    # The holder finishing hands the device to nobody that left.
    assert broker.exclusive_release(holder["lease_id"])
    time.sleep(0.3)
    assert broker.exclusive_store.get("abandoned") is None
    assert broker.exclusive_store.active("gpu0") == []


def test_a_draining_exclusive_caller_that_hangs_up_fails_its_drain(serve):
    broker = _broker([_BusyPeer("http://alpha", "alpha")], time.monotonic)
    port = serve(broker)

    caller = _post(port, {**_exclusive_body("abandoned"), "wait_s": 60, "wait_timeout_s": 60})
    assert _until(lambda: (broker.exclusive_store.get("abandoned") or {}).get("state") == "draining")
    caller.close()
    assert _until(lambda: broker.exclusive_store.get("abandoned")["state"] == "failed")
    record = broker.exclusive_store.get("abandoned")
    assert record["reason"] == "caller_gone"
    assert broker.exclusive_store.active("gpu0") == []


def test_a_held_admission_whose_caller_hangs_up_stops_waiting(serve):
    alpha = _Peer("http://alpha", "alpha", resident=False)
    broker = _broker([alpha], time.monotonic)
    port = serve(broker)
    holder = _admit(port, _exclusive_body("holder"))
    assert holder["granted"] is True

    caller = _post(port, {"id": "abandoned-plain", "kind": "alpha", "owner": "bob", "wait_s": 60})
    assert _until(lambda: "abandoned-plain" in broker._admission_waiters)
    caller.close()
    assert _until(lambda: "abandoned-plain" not in broker._admission_waiters)
    assert ("warm", "alpha") not in alpha.calls


def test_a_cancelled_exclusive_lease_wait_is_cut_and_hostd_drops_it(serve):
    """End to end: client.exclusive_lease's queued wait, cut by its Cancel."""
    from livestack_node import transport
    from livestack_node.client import exclusive_lease

    broker = _broker([_Peer("http://alpha", "alpha", resident=False)], time.monotonic)
    port = serve(broker)
    base = f"http://127.0.0.1:{port}"
    holder = _admit(port, _exclusive_body("holder"))
    assert holder["granted"] is True
    cancel = transport.Cancel()
    outcome = []

    def wait():
        try:
            with exclusive_lease("alpha", device_id="gpu0", min_free_bytes=24_000, owner_id="bob",
                                 brokers=[base], request_id="cut", queue_wait_s=60, cancel=cancel):
                outcome.append("granted")
        except transport.Cancelled:
            outcome.append("cancelled")

    waiter = threading.Thread(target=wait)
    waiter.start()
    assert _until(lambda: "cut" in broker._exclusive_waiters.get("gpu0", []))
    started = time.monotonic()
    cancel.cancel()
    waiter.join(timeout=5)
    assert outcome == ["cancelled"] and time.monotonic() - started < 1.0
    assert _until(lambda: "cut" not in broker._exclusive_waiters.get("gpu0", []))
