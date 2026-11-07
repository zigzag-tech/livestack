"""Every way a leased image request ends gives its device leases back.

xc-tower-ubuntu, 2026-11-03: histo's chain was killed mid-request. The gateway
kept serving the abandoned request, took both GPUs and ran a FLUX job for
nobody while the chain's retry waited behind it.
"""
import json
import socket
import threading
import time

import pytest

from livestack_node import transport
from livestack_node.client import LeaseLost
from livestack_node.imagegen.gateway import create_app

from test_imagegen_gateway import _guided_body, _guided_call, setup  # noqa: F401

uvicorn = pytest.importorskip("uvicorn")

DEVICES = ["tower/gpu1", "tower/gpu0"]


def _leased(config, body, events, *, refuse=None, on_enter=None, worker=None, residence=None):
    """Configure a two-device lease profile and return (lease factory, call)."""
    config["exclusive_units"] = {"qwen_image_2512_depth_nf4": {
        "broker": "http://host-broker", "owner": "harmony-image", "device_ids": DEVICES,
        "min_free_bytes": 1, "wait_timeout_s": 240, "ttl_seconds": 120}}
    original = _guided_call(body, [])

    class Lease:
        def __init__(self, kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            device = self.kwargs["device_id"]
            if on_enter is not None:
                on_enter(device, self.kwargs["cancel"])
            if device == refuse:
                raise LeaseLost("exclusive admission refused: waited 3507s of 3506.94s")
            events.append("enter:" + device)
            return {"granted": True, "lease_id": "device-lease-" + device[-1],
                    "device_id": device, "state": "exclusive"}

        def __exit__(self, *args):
            events.append("exit:" + self.kwargs["device_id"])

    def call(base, method, path, request=None, token=None, timeout=None, lease_id=None,
             cancel=None):
        if path == "/livestack/residence":
            events.append("residence")
            return residence() if residence is not None else {"units": [{"kind": "u", "busy": False}]}
        if path == "/v1/images/generations":
            events.append("worker")
            if worker is not None:
                worker(cancel)
        result = original(base, method, path, request, token=token)
        if path == "/v1/images/generations":
            result["harmony"]["device_leases"] = [
                {"lease_id": "device-lease-" + device[-1], "device_id": device} for device in DEVICES]
        return result

    return Lease, call


def _post(client_app, body):
    from fastapi.testclient import TestClient
    return TestClient(client_app).post("/v1/images/generations", json=body,
                                       headers={"Authorization": "Bearer worker-secret"})


def test_a_refused_second_device_releases_the_first(setup, monkeypatch):  # noqa: F811
    config, _, _ = setup
    body = {**_guided_body(), "harmony_wait_s": 60}
    events = []
    Lease, call = _leased(config, body, events, refuse="tower/gpu1")
    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", lambda *a, **kw: Lease(kw))
    response = _post(create_app(config, call=call), body)
    assert response.status_code == 503 and "3507s" in response.json()["detail"]
    assert events == ["enter:tower/gpu0", "exit:tower/gpu0"]


def test_a_failed_worker_call_releases_every_lease(setup, monkeypatch):  # noqa: F811
    config, _, _ = setup
    body = {**_guided_body(), "harmony_wait_s": 60}
    events = []

    def worker(cancel):
        raise RuntimeError("Harmony HTTP 500: out of memory")

    Lease, call = _leased(config, body, events, worker=worker)
    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", lambda *a, **kw: Lease(kw))
    response = _post(create_app(config, call=call), body)
    assert response.status_code == 503
    assert sorted(e for e in events if e.startswith("exit:")) == ["exit:tower/gpu0", "exit:tower/gpu1"]


@pytest.fixture
def gateway(setup):  # noqa: F811
    servers = []

    def start(config, call):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(config, call=call), host="127.0.0.1",
                                               port=port, log_level="error", lifespan="off"))
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


def _hang_up_after(port, body, ready, timeout=5.0):
    data = json.dumps(body).encode()
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"POST /v1/images/generations HTTP/1.1\r\nHost: test\r\n"
                 b"Authorization: Bearer worker-secret\r\nContent-Type: application/json\r\n"
                 + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    assert ready.wait(timeout)
    sock.close()


def _wait_for_cancel(cancel, limit=10.0):
    deadline = time.monotonic() + limit
    while not cancel.cancelled:
        assert time.monotonic() < deadline, "the gateway never noticed its caller hang up"
        time.sleep(0.02)
    raise transport.Cancelled("cancelled")


def test_a_caller_that_hangs_up_while_queued_stops_the_lease_wait(setup, gateway, monkeypatch):  # noqa: F811
    config, _, _ = setup
    body = {**_guided_body(), "harmony_wait_s": 3600}
    events, waiting, cut = [], threading.Event(), []

    def on_enter(device, cancel):
        if device == "tower/gpu1":          # holds gpu0, queues for gpu1
            waiting.set()
            started = time.monotonic()
            try:
                _wait_for_cancel(cancel)
            finally:
                cut.append(time.monotonic() - started)

    Lease, call = _leased(config, body, events, on_enter=on_enter)
    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", lambda *a, **kw: Lease(kw))
    port = gateway(config, call)
    _hang_up_after(port, body, waiting)
    deadline = time.monotonic() + 5
    while "exit:tower/gpu0" not in events and time.monotonic() < deadline:
        time.sleep(0.02)
    assert events == ["enter:tower/gpu0", "exit:tower/gpu0"]
    assert cut and cut[0] < 3.0
    assert "worker" not in events


def test_a_caller_that_hangs_up_mid_generation_cuts_it_and_releases(setup, gateway, monkeypatch):  # noqa: F811
    config, _, _ = setup
    body = {**_guided_body(), "harmony_wait_s": 3600}
    events, running = [], threading.Event()

    def worker(cancel):
        running.set()
        _wait_for_cancel(cancel)

    Lease, call = _leased(config, body, events, worker=worker)
    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", lambda *a, **kw: Lease(kw))
    port = gateway(config, call)
    _hang_up_after(port, body, running)
    deadline = time.monotonic() + 5
    while len([e for e in events if e.startswith("exit:")]) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sorted(e for e in events if e.startswith("exit:")) == ["exit:tower/gpu0", "exit:tower/gpu1"]


def test_a_cut_worker_call_keeps_the_leases_until_the_worker_is_idle(setup, gateway, monkeypatch):  # noqa: F811
    """xc-tower-ubuntu, 2026-11-07 06:43 and 08:47: the gateway released both
    leases the moment its caller hung up, while the FLUX.2 worker generated on
    both cards for another 12-15 minutes; the next request's drain saw that
    memory as external and timed out at 240 s. The leases now outlive the cut
    until the worker's facade reports every unit idle."""
    config, _, _ = setup
    body = {**_guided_body(), "harmony_wait_s": 3600}
    events, running = [], threading.Event()
    reports = iter([True, True, False])

    def worker(cancel):
        running.set()
        _wait_for_cancel(cancel)

    def residence():
        return {"units": [{"kind": "qwen_image_2512_depth_nf4", "busy": next(reports)}]}

    monkeypatch.setattr("livestack_node.imagegen.gateway.WORKER_IDLE_POLL_S", 0.01)
    Lease, call = _leased(config, body, events, worker=worker, residence=residence)
    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", lambda *a, **kw: Lease(kw))
    port = gateway(config, call)
    _hang_up_after(port, body, running)
    deadline = time.monotonic() + 5
    while len([e for e in events if e.startswith("exit:")]) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    first_exit = min(i for i, e in enumerate(events) if e.startswith("exit:"))
    assert events[first_exit - 3:first_exit] == ["residence"] * 3
    assert sorted(e for e in events if e.startswith("exit:")) == ["exit:tower/gpu0", "exit:tower/gpu1"]


def test_an_unanswering_facade_does_not_hold_the_leases_to_the_deadline(monkeypatch):
    from livestack_node.imagegen import gateway as module

    def call(*a, **k):
        raise OSError("connection refused")
    started = time.monotonic()
    assert module._await_worker_idle(call, "http://w", time.monotonic() + 60,
                                     poll_s=0.01, unreachable_s=0.1) is False
    assert time.monotonic() - started < 2


def test_a_facade_whose_worker_has_exited_counts_as_idle():
    """xc-tower-ubuntu, 2026-11-07 09:45-10:02: the FLUX worker stopped on the
    cut and exited, but its facade still reported busy (a stale last_error), and
    the gateway held both leases until the facade was restarted."""
    from livestack_node.imagegen import gateway as module
    reports = []

    def call(base, method, path, *a, **k):
        reports.append(path)
        return {"units": [{"kind": "flux", "busy": True}],
                "worker": {"running": False, "pid": None, "last_error": "worker_state_unknown"}}
    assert module._await_worker_idle(call, "http://w", time.monotonic() + 60, poll_s=0.01) is True
    assert reports == ["/livestack/residence"]
