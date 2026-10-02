"""Per-unit admission (design §4a): `max_concurrent` in flight, a bounded FIFO
of waiters, and a 429 with the queue state beyond the bound.

The engine's own admission limit is the number — `--max-num-seqs` for vLLM,
`--parallel` for Strata — and the queue exists so requests stop piling into an
engine that is already at it. A unit with a queue of its own is not a shortcut
for the router either: the resident-reuse optimisation applies only below the
limit with an EMPTY queue.
"""
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from unit_queue import DEFAULT_MAX_WAITING, Queues, QueueFull, UnitQueue

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


# -- the queue itself --------------------------------------------------------

def test_at_most_max_concurrent_in_flight_and_the_rest_wait():
    q = UnitQueue("llm", max_concurrent=1)
    a = q.acquire()
    assert q.has_capacity() is False
    order = []
    t = threading.Thread(target=lambda: (order.append(q.acquire()), order[-1].release()))
    t.start()
    time.sleep(0.1)
    assert order == []                       # second request WAITS, not errors
    a.release()
    t.join(2)
    assert len(order) == 1
    assert order[0].queue_ms > 50            # the wait is measured, not implied


def test_the_bound_is_bounded_fifo_and_the_overflow_names_the_state():
    q = UnitQueue("llm", max_concurrent=1, max_waiting=2)
    held = q.acquire()
    go = threading.Event()

    def waiter():
        slot = q.acquire()
        go.wait(5)
        slot.release()

    ts = [threading.Thread(target=waiter) for _ in range(2)]
    for t in ts:
        t.start()
    deadline = time.time() + 5
    while q.status()["waiting"] < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert q.status()["waiting"] == 2         # both waiters are queued
    with pytest.raises(QueueFull) as e:
        q.acquire()
    assert "queue is full" in str(e.value)    # the reason a caller is shown
    assert "waiting" in str(e.value) and "max_concurrent" in str(e.value)
    held.release()
    go.set()
    for t in ts:
        t.join(5)
    assert q.status()["in_flight"] == 0


def test_default_bound_is_64():
    q = UnitQueue("llm", max_concurrent=1)
    assert q.max_waiting == DEFAULT_MAX_WAITING == 64


def test_release_is_idempotent():
    q = UnitQueue("llm", max_concurrent=1)
    slot = q.acquire()
    slot.release()
    slot.release()                           # three response paths may all try
    assert q.status() == {"in_flight": 0, "waiting": 0,
                          "max_concurrent": 1, "max_waiting": 64}


def test_capacity_is_empty_queue_below_the_limit():
    q = UnitQueue("llm", max_concurrent=2)
    assert q.has_capacity() is True
    a, b = q.acquire(), q.acquire()
    assert q.has_capacity() is False         # at the engine's limit
    a.release()
    assert q.has_capacity() is True
    b.release()


def test_a_unit_nobody_asked_for_has_capacity():
    assert Queues().has_capacity("flash_next", 4) is True


# -- through the real proxy, with a stub engine that sleeps -------------------

class _Sleepy(BaseHTTPRequestHandler):
    delay = 0.25
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Sleepy.seen.append(body)
        time.sleep(_Sleepy.delay)            # an engine at work
        out = json.dumps({"model": body.get("model"),
                          "choices": [{"message": {"content": "4"}}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("queue")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_serial", "model": "m/serial", "port": 8189,
         "footprint_gb": 5, "max_model_len": "2048",
         "extra_args": "--max-num-seqs 1",
         "attributes": {"class": "llm", "params_b": 1}},
    ]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_DEMAND_LOG_AGE_DAYS": "21",
        "HARMONY_DEMAND_LOG_DIR": str(root / "demand"),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_queue", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_queue"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


@pytest.fixture()
def upstream(srv, monkeypatch):
    _Sleepy.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Sleepy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(srv, "_base_of", lambda name: base)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv.manager, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_vllm_up", lambda **k: True)
    monkeypatch.setattr(srv, "admit", lambda *a, **k: {"kind": "llm_serial", "granted": True,
                                                        "device_id": srv.DEVICE_ID_SELF})
    yield base
    httpd.shutdown()


def test_two_requests_share_one_engine_slot_and_one_waits(srv, upstream):
    from fastapi.testclient import TestClient
    results = {}

    def call(i):
        client = TestClient(srv.app)
        r = client.post("/v1/chat/completions",
                        json={"model": "local", "messages": [{"role": "user", "content": "x"}]})
        results[i] = r.status_code

    ts = [threading.Thread(target=call, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert results == {0: 200, 1: 200}
    assert len(_Sleepy.seen) == 2
    srv.DEMAND.flush()
    rows = [json.loads(l) for l in Path(srv.DEMAND.path).read_text().splitlines()]
    waits = [r.get("queue_ms") or 0 for r in rows[-2:]]
    # ONE of them waited behind the other (engine `max_concurrent=1`), and the
    # record says HOW LONG — a saturated unit is visible in its own demand log.
    assert max(waits) > 100


def test_queue_depth_is_on_the_residence_report(srv, upstream):
    depth = srv._UNITS["llm_serial"].extra_report()["queue"]
    assert set(depth) == {"in_flight", "waiting", "max_concurrent", "max_waiting"}
    assert depth["max_concurrent"] == 1


def test_the_overflow_is_a_429_naming_the_queue_state(srv, upstream, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(srv, "_QUEUES", Queues(max_waiting=1))
    held = srv._QUEUES.acquire("llm_serial", 1)     # the engine is busy
    blocker = threading.Thread(target=lambda: srv._QUEUES.acquire("llm_serial", 1))
    blocker.start()
    deadline = time.time() + 5
    while srv._QUEUES.status("llm_serial", 1)["waiting"] < 1 and time.time() < deadline:
        time.sleep(0.01)                             # one waiter fills the bound
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "local", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 429
    assert "queue is full" in r.text
    held.release()
    blocker.join(5)
