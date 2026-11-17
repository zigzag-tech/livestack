"""A router whose fleet view has a background refresher never fetches on the
request path.

2026-11-17 14:05 CST: router-zz-tower2-speech accepted a China dictation's
WebSocket and chose an engine 6 s later. Its /fleet broker is in Vaughan
(2-9 s per GET), the cached view had aged past _VIEW_TTL_S, and `fleet_view`
then did a synchronous GET on the event loop. The client's 6 s handshake
timeout fired first. These tests pin the request path to the cache when a
refresher owns the URL, and keep the old fetch-on-miss behaviour otherwise.
"""
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"
sys.path.insert(0, str(HERE))
import fleet_route  # noqa: E402

URL = "http://broker.test:8801"
VIEW = {"hosts": {"xc-mac-studio": {"nodes": []}}, "generated_at": 1.0}


@pytest.fixture
def slow_broker(monkeypatch):
    """A broker that takes 2 s and then fails, counting request-path calls."""
    calls = []

    def slow_get(url, timeout=None):
        calls.append(url)
        time.sleep(2.0)
        raise RuntimeError("broker timed out")

    monkeypatch.setattr(fleet_route.httpx, "get", slow_get)
    monkeypatch.setattr(fleet_route, "_cache", {})
    monkeypatch.setattr(fleet_route, "_refreshed", set())
    return calls


def _age(seconds):
    fleet_route._cache[URL] = (time.time() - seconds, VIEW)


def test_with_a_refresher_a_stale_view_is_served_without_fetching(slow_broker):
    fleet_route._refreshed.add(URL)
    _age(fleet_route._VIEW_TTL_S + 5)          # past the TTL, inside STALE_MAX_S
    t0 = time.monotonic()
    assert fleet_route.fleet_view(URL) is VIEW
    assert time.monotonic() - t0 < 0.2, "the request path must not wait on the broker"
    assert slow_broker == [], "no GET on the request path"


def test_with_a_refresher_and_no_usable_view_the_answer_is_none_at_once(slow_broker):
    fleet_route._refreshed.add(URL)
    _age(fleet_route.STALE_MAX_S + 1)          # too old to stand in
    t0 = time.monotonic()
    assert fleet_route.fleet_view(URL) is None
    assert time.monotonic() - t0 < 0.2
    assert slow_broker == []


def test_without_a_refresher_a_miss_still_fetches(slow_broker):
    """Positive control: the old path is intact for callers that run no
    refresher, and this fixture really does make the request path wait."""
    _age(fleet_route._VIEW_TTL_S + 5)
    t0 = time.monotonic()
    assert fleet_route.fleet_view(URL) is VIEW   # failed fetch -> last good view
    assert time.monotonic() - t0 >= 1.9
    assert slow_broker == [URL + "/fleet"]


def test_start_refresher_registers_the_url_and_stopping_releases_it(monkeypatch):
    monkeypatch.setattr(fleet_route, "_cache", {})
    monkeypatch.setattr(fleet_route, "_refreshed", set())

    class Resp:
        def json(self):
            return VIEW

    monkeypatch.setattr(fleet_route.httpx, "get", lambda url, timeout=None: Resp())
    stop = fleet_route.start_refresher(URL, every_s=0.05, timeout_s=1.0)
    assert URL in fleet_route._refreshed
    deadline = time.monotonic() + 2
    while URL not in fleet_route._cache and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fleet_route._cache[URL][1] is VIEW
    stop.set()
    deadline = time.monotonic() + 2
    while URL in fleet_route._refreshed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert URL not in fleet_route._refreshed
