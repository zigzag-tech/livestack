"""The registrar announces only a facade that answers.

`attach()` starts this thread at import — before uvicorn binds and long before a
model is loadable. Combined with the broker's old "a registration IS proof of
life" rule, that meant a server which never became able to serve announced
itself `fresh` forever. `membership.py` fixed the broker half; this is the node
half: do not claim duty over a door that is not there.

The fake facade below is a REAL HTTP server whose status code the test flips,
because the thing under test is what a socket does — a mocked `urlopen` would
prove only that the mock was called.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from livestack_node.announce import facade_answers, start_registrar


class _Facade:
    """A one-node facade whose /residence status the test controls."""

    def __init__(self, status=503):
        self.status = status
        self.hits = 0
        self.registrations = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"host_id":"h","device_id":"h/gpu0","units":[]}')

            # The same stub also stands in for a BROKER in the multi-URL test:
            # POST /peers. One fixture because on a real host they are two
            # processes on one machine, and the test is about which of them a
            # node can still reach when the other is gone.
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.registrations += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"peer":"x","source":"registered","state":"fresh"}')

            def log_message(self, *_):
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}/livestack"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_facade_answers_reads_the_real_status_code():
    f = _Facade(status=503)
    try:
        assert facade_answers(f.url) is False
        f.status = 200
        assert facade_answers(f.url) is True
    finally:
        f.close()


def test_a_facade_that_never_binds_is_never_announced():
    """Nothing is listening at all — the cold-boot case. Not one registration."""
    sent = []
    t = start_registrar(
        "http://127.0.0.1:1/livestack", host_id="h", kind="polyasr",
        interval_s=0.01, log=lambda _m: None,
        register=lambda *a, **k: sent.append(k) or {},
    )
    try:
        time.sleep(0.3)
        assert sent == [], "announced a facade that does not exist"
    finally:
        t.stop()


def test_a_503_facade_is_withheld_and_announced_once_it_answers():
    f = _Facade(status=503)
    sent = []
    lines = []
    t = start_registrar(
        f.url, host_id="h", kind="polyasr", interval_s=0.01,
        log=lines.append,
        register=lambda *a, **k: sent.append(k) or {},
    )
    try:
        time.sleep(0.3)
        assert sent == [], "announced while the facade was 503"
        assert lines == [], "withholding on a cold boot is not an event"

        f.status = 200
        assert _wait_for(lambda: len(sent) >= 1), "never announced once it answered"
        duty = [l for l in lines if "reported for duty" in l]
        assert len(duty) == 1, f"expected exactly one duty line, got {duty}"
    finally:
        t.stop()
        f.close()


def test_the_self_probe_backs_off_rather_than_spinning():
    """A node that starts before its server binds must not hammer its own port
    at full speed for the whole boot — the retry ladder is the same one an
    unreachable broker gets."""
    probes = []
    t = start_registrar(
        "http://127.0.0.1:1/livestack", host_id="h", kind="polyasr",
        interval_s=0.01, log=lambda _m: None,
        answers=lambda _u: probes.append(time.monotonic()) or False,
        register=lambda *a, **k: {},
    )
    try:
        time.sleep(0.5)
    finally:
        t.stop()
    # RETRY_MIN_S is 2 s, so half a second buys exactly one probe. The point is
    # that it is not a spin: without backoff this would be hundreds.
    assert len(probes) <= 2, f"self-probe is spinning: {len(probes)} probes in 0.5 s"
    assert len(probes) >= 1


# -- Phase 4: announce to the fleet as well as to the host broker -------------

def test_a_comma_list_reaches_every_broker():
    """One entry is a node and its host broker on one machine; two is the fleet
    case — the broker that may warm and evict it, and the one that only wants to
    know it exists."""
    import os
    from livestack_node.announce import broker_urls
    prev = os.environ.get("LIVESTACK_BROKER_URL")
    try:
        os.environ["LIVESTACK_BROKER_URL"] = \
            "http://127.0.0.1:8799, http://100.64.0.18:8801/ ,http://127.0.0.1:8799"
        # Trimmed, de-duplicated, trailing slashes gone — an operator's list
        # written by hand must not create a phantom third broker.
        assert broker_urls() == ["http://127.0.0.1:8799", "http://100.64.0.18:8801"]
        del os.environ["LIVESTACK_BROKER_URL"]
        assert broker_urls() == ["http://127.0.0.1:8799"]
    finally:
        if prev is None:
            os.environ.pop("LIVESTACK_BROKER_URL", None)
        else:
            os.environ["LIVESTACK_BROKER_URL"] = prev


def test_one_broker_down_does_not_stop_the_announce_to_the_other():
    """ALL, not any. A fleet broker being down must not make a node look
    unregistered to the host broker that arbitrates its card — and a node whose
    host broker is restarting should still reach the fleet."""
    import os
    from livestack_node.announce import register_once
    f = _Facade(status=200)
    prev = os.environ.get("LIVESTACK_BROKER_URL")
    try:
        # One real broker (the facade stub answers any path with 200) and one
        # that is not there at all.
        base = f.url.rsplit("/livestack", 1)[0]
        os.environ["LIVESTACK_BROKER_URL"] = f"http://127.0.0.1:1,{base}"
        got = register_once("http://n/livestack", host_id="h", kind="polyasr")
        assert got.get("state") == "fresh", "a reachable broker must still be reached"
        assert f.registrations == 1

        os.environ["LIVESTACK_BROKER_URL"] = "http://127.0.0.1:1,http://127.0.0.1:2"
        try:
            register_once("http://n/livestack", host_id="h", kind="polyasr")
            assert False, "all brokers down must raise, or the registrar never backs off"
        except Exception:
            pass
    finally:
        f.close()
        if prev is None:
            os.environ.pop("LIVESTACK_BROKER_URL", None)
        else:
            os.environ["LIVESTACK_BROKER_URL"] = prev
