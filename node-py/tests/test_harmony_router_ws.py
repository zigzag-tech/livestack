"""Streaming ASR through the router (/ws/transcribe): placement before the
session is committed, replay of the client's audio on failover, and native
streaming as a preference (openspec harmony-speech-routing). Real sockets."""
import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")
uvicorn = pytest.importorskip("uvicorn")
from websockets.asyncio.server import serve  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"
sys.path.insert(0, str(HERE))
import fleet_route  # noqa: E402


@pytest.fixture(scope="module")
def router():
    os.environ.setdefault("HARMONY_ROUTER_ID", "test-router")
    spec = importlib.util.spec_from_file_location("harmony_router_ws_under_test", HERE / "router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_router_ws_under_test"] = module
    spec.loader.exec_module(module)
    return module


class Loop:
    """An event loop on a thread hosting fake nodes and the router."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(15)


class FakeNode:
    def __init__(self, lp, mode="ok"):
        self.got, self.mode, self.starts, self.closed = [], mode, 0, 0

        async def handler(ws):
            try:
                await serve_one(ws)
            finally:
                self.closed += 1

        async def serve_one(ws):
            async for m in ws:
                self.got.append(m)
                if isinstance(m, str) and json.loads(m).get("type") == "start":
                    self.starts += 1
                    await ws.send(json.dumps({"type": "started", "protocol": 1, "sessionId": "s", "ackSeq": -1}))
                    if self.mode == "loading":
                        # a cold admission: the node's own warming notes (with extras a client must not see)
                        for i in range(3):
                            await asyncio.sleep(0.3)
                            await ws.send(json.dumps({"type": "warming", "sessionId": "s", "elapsedMs": 300 * (i + 1),
                                                      "expectedMs": 15000, "engine": "ENGINE-ID-7f3a"}))
                        continue
                    if self.mode == "busy":
                        await ws.send(json.dumps({"type": "error", "error": "Harmony did not grant this device: held by image"}))
                        await ws.close(code=1013, reason="busy")
                        return
                elif isinstance(m, bytes) and self.mode == "errlate":
                    await ws.send(json.dumps({"type": "partial", "text": "x"}))
                    await ws.send(json.dumps({"type": "error", "error": "engine ENGINE-ID-7f3a on gpu-host-secret.internal OOM"}))
                    await ws.close(code=1011, reason="gpu-host-secret.internal crashed")
                    return
                elif isinstance(m, bytes) and self.mode == "leaky":
                    # seeded leaks: extra fields on every frame type, a typeless exception frame,
                    # a frame of an unknown type, a binary frame
                    await ws.send(b"BIN-ENGINE-ID-7f3a")
                    await ws.send(json.dumps({"type": "gpu-status", "gpu": "GPU-SECRET-0"}))
                    await ws.send(json.dumps({"type": "partial", "partial": "hi", "delta": "hi", "sessionId": "s",
                                              "engine": "ENGINE-ID-7f3a", "host": "gpu-host-secret.internal"}))
                    await ws.send(json.dumps({"type": "final", "sessionId": "s", "stopId": "x", "text": "hi",
                                              "recognizedThroughSeq": 3, "model": "MODEL-SECRET-large", "gpu": "GPU-SECRET-0"}))
                    await ws.send(json.dumps({"error": "weights at /models/MODEL-SECRET-large missing"}))
                    await ws.send(json.dumps({"type": "done", "sessionId": "s", "stopId": "x", "device": "GPU-SECRET-0"}))
                elif isinstance(m, bytes) and self.mode == "ok":
                    await ws.send(json.dumps({"type": "partial", "text": "SECRET-TRANSCRIPT", "from": self.port}))

        self.srv = lp.run(self._start(handler))
        self.port = self.srv.sockets[0].getsockname()[1]

    async def _start(self, handler):
        return await serve(handler, "127.0.0.1", 0)

    def row(self, streaming="native", in_flight=0, resident=True):
        return {"peer": f"http://127.0.0.1:{self.port}/livestack", "state": "fresh", "ready": True,
                "kinds": ["polyasr"], "region": "na", "detail": "x", "load": {"in_flight": in_flight},
                "inventory": {"asr": {"streaming": streaming}},
                "units": [{"kind": "asr", "resident": resident}]}


@pytest.fixture
def world(router, monkeypatch):
    lp = Loop()
    nodes = []
    started = []

    def build(*specs):
        view = {"generated_at": time.time(), "hosts": {}}
        mine = []
        for i, (mode, row) in enumerate(specs):
            n = FakeNode(lp, mode)
            nodes.append(n)
            mine.append(n)
            view["hosts"][f"h{i}"] = {"nodes": [n.row(**row)]}
        monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
        monkeypatch.setattr(fleet_route, "mark_busy", lambda *a: None)
        monkeypatch.setattr(router, "_PRINCIPALS", None)
        cfg = uvicorn.Config(router.app, host="127.0.0.1", port=0, log_level="warning", loop="asyncio")
        srv = uvicorn.Server(cfg)
        lp.run(_serve(srv, started))
        return f"ws://127.0.0.1:{started[-1]}/ws/transcribe", mine

    yield build
    for n in nodes:
        n.srv.close()


async def _serve(srv, started):
    asyncio.ensure_future(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.02)
    started.append(srv.servers[0].sockets[0].getsockname()[1])


START = json.dumps({"type": "start", "protocol": 1, "sessionId": "s"})


async def _dictate(url, audio=b"\x00" * 64, wait=3.0, start=None):
    out = []
    async with connect(url) as ws:
        await ws.send(start or START)
        out.append(json.loads(await asyncio.wait_for(ws.recv(), wait)))      # the client's ack
        await ws.send(audio)
        try:
            while True:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), wait)))
                if out[-1]["type"] in ("partial", "error"):
                    break
        except Exception as e:
            out.append({"type": "closed", "why": repr(e)[:80]})
    return out


def test_a_session_refused_after_its_ack_fails_over_and_the_audio_is_replayed(world):
    url, (busy, ok) = world(("busy", {"in_flight": 0}), ("ok", {"in_flight": 1}))
    lp = Loop()
    frames = lp.run(_dictate(url))
    types = [f["type"] for f in frames]
    assert types[0] == "started" and "partial" in types and "error" not in types
    assert types.count("started") == 1                     # the second node's ack was swallowed
    assert busy.starts == 1 and ok.starts == 1
    assert ok.got[1] == b"\x00" * 64                         # audio replayed to the node that served it


def test_every_provider_refusing_ends_in_a_generic_error_frame_and_the_log_names_each_try(world, capfd):
    url, nodes = world(("busy", {"in_flight": 0}), ("busy", {"in_flight": 1}))
    start = json.dumps({"type": "start", "protocol": 1, "sessionId": "s"})
    frames = Loop().run(_dictate(url))
    err = [f for f in frames if f["type"] == "error"][-1]
    assert err["error"] == "unavailable" and err["retryAfterS"] == 30 and err["requestId"]
    assert "attempts" not in err and "did not grant" not in json.dumps(frames)
    assert [n.starts for n in nodes] == [1, 1]
    logged = [json.loads(l) for l in capfd.readouterr().out.splitlines() if "router_public_error" in l]
    assert logged[-1]["request_id"] == err["requestId"] and "did not grant" in logged[-1]["detail"]


def test_the_close_carries_a_generic_code_and_reason_only(world):
    url, nodes = world(("busy", {"in_flight": 0}))

    async def go():
        async with connect(url) as ws:
            await ws.send(START)
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), 3)
            except Exception as e:
                return e

    closed = Loop().run(go())
    assert closed.rcvd.code == 1013 and closed.rcvd.reason == "unavailable"
    for leak in ("did not grant", "busy", "127.0.0.1", str(nodes[0].port)):
        assert leak not in closed.rcvd.reason


def test_a_node_error_frame_and_close_after_admission_are_scrubbed(world):
    url, (node,) = world(("errlate", {}))

    async def go():
        seen, closed = [], None
        async with connect(url) as ws:
            await ws.send(START)
            await ws.send(b"\x00" * 8)
            try:
                while True:
                    seen.append(await asyncio.wait_for(ws.recv(), 3))
            except Exception as e:
                closed = e
        return seen, closed

    seen, closed = Loop().run(go())
    blob = json.dumps(seen) + str(getattr(closed, "rcvd", ""))
    types = [json.loads(m).get("type") for m in seen]
    assert "partial" in types and types[-1] == "error"               # committed, then failed
    assert json.loads(seen[-1])["error"] == "unavailable"
    for leak in ("ENGINE-ID-7f3a", "gpu-host-secret.internal", "OOM"):
        assert leak not in blob, leak
    assert closed.rcvd.code == 1011 and closed.rcvd.reason == "unavailable"


def test_native_streaming_is_a_preference_not_a_requirement(world):
    # equal distance and load: native wins the tie ...
    url, (syn, nat) = world(("ok", {"streaming": "synthetic"}), ("ok", {"streaming": "native"}))
    frames = Loop().run(_dictate(url))
    # the client frame carries no node identity (whitelist): see which node received the audio
    assert any(isinstance(m, bytes) for m in nat.got) and not any(isinstance(m, bytes) for m in syn.got)
    assert any(f["type"] == "partial" for f in frames)
    # ... and a synthetic-only fleet still serves
    url, (only,) = world(("ok", {"streaming": "synthetic"}))
    assert any(f["type"] == "partial" for f in Loop().run(_dictate(url))) and any(isinstance(m, bytes) for m in only.got)


def test_the_decision_log_holds_features_and_outcomes_never_audio_transcript_or_context(world, router, monkeypatch, tmp_path):
    """Privacy: what the router logs about a dictation is which node, why, how long.
    Audio, the transcript the node returned and the `context` string (pane names,
    recent turns) must appear in NO log row, with a failover so attempts/causes
    are exercised too. Checked on the real bounded log file, not a fake."""
    from livestack_node import placement
    from livestack_node.demand_log import demand_log_from_env
    monkeypatch.setenv("HARMONY_DEMAND_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("HARMONY_DEMAND_LOG_AGE_DAYS", "1")
    log = demand_log_from_env("placement")
    placement.set_decision_log(log)
    routes = demand_log_from_env("routes")
    monkeypatch.setattr(router, "ROUTES", routes)
    try:
        url, (busy, ok) = world(("busy", {"in_flight": 0}), ("ok", {"in_flight": 1}))
        start = json.dumps({"type": "start", "protocol": 1, "sessionId": "s",
                            "context": "SECRET-CONTEXT /home/me/private-pane"})
        frames = Loop().run(_dictate(url, audio=b"SECRET-AUDIO" * 8, start=start))
        assert any(f["type"] == "partial" for f in frames)       # the dictation really flowed
        placement.record_outcome("x", target="t", success=True, first_byte_ms=1.0)
        log.flush()
        routes.flush()
        text = b"".join(p.read_bytes() for p in tmp_path.glob("*.jsonl*"))
        assert b"placement" in text or text                       # the instrument wrote something
        assert b'"event": "decision"' in text or b'"event":"decision"' in text
        for secret in (b"SECRET-AUDIO", b"SECRET-TRANSCRIPT", b"SECRET-CONTEXT", b"private-pane"):
            assert secret not in text, secret
        # positive control: the sentinels did reach the nodes, so absence is meaningful
        assert any(b"SECRET-AUDIO" in m for m in ok.got if isinstance(m, bytes))
    finally:
        placement.set_decision_log(None)


def test_frames_are_whitelisted_per_type_so_node_extras_never_reach_the_client(world):
    url, (node,) = world(("leaky", {}))

    async def go():
        seen = []
        async with connect(url) as ws:
            await ws.send(START)
            await ws.send(b"\x00" * 8)
            try:
                while True:
                    seen.append(await asyncio.wait_for(ws.recv(), 1.5))
            except Exception:
                pass
        return seen

    seen = Loop().run(go())
    blob = json.dumps([m if isinstance(m, str) else "<bytes>" for m in seen])
    frames = [json.loads(m) for m in seen]
    for leak in ("ENGINE-ID-7f3a", "gpu-host-secret", "MODEL-SECRET", "GPU-SECRET", "/models", "gpu-status"):
        assert leak not in blob, leak
    # positive control: the protocol fields survive, so the whitelist is not simply dropping everything
    by = {f["type"]: f for f in frames}
    assert by["partial"] == {"type": "partial", "sessionId": "s", "partial": "hi", "delta": "hi"}
    assert by["final"] == {"type": "final", "sessionId": "s", "stopId": "x", "text": "hi", "recognizedThroughSeq": 3}
    assert by["done"] == {"type": "done", "sessionId": "s", "stopId": "x"}
    assert by["error"]["error"] == "unavailable"                     # the typeless exception frame


def _frame_times(url, wait=4.0, until=("partial", "error")):
    """(frame, seconds since start) for each frame until one of `until` arrives."""
    async def go():
        out, t0 = [], time.time()
        async with connect(url) as ws:
            await ws.send(START)
            await ws.send(b"\x00" * 64)
            try:
                while True:
                    f = json.loads(await asyncio.wait_for(ws.recv(), wait))
                    out.append((f, time.time() - t0))
                    if f["type"] in until:
                        break
            except Exception:
                pass
        return out
    return Loop().run(go())


def test_a_cold_provider_gets_a_router_warming_frame_right_after_started(world):
    url, (node,) = world(("ok", {"resident": False}))
    frames = _frame_times(url)
    types = [f["type"] for f, _ in frames]
    assert types[:2] == ["started", "warming"] and "partial" in types
    warming, at = frames[1]
    assert at < 1.0
    assert warming["protocol"] == 1 and warming["sessionId"] == "s" and isinstance(warming["elapsedMs"], int)
    assert set(warming) <= {"type", "protocol", "sessionId", "elapsedMs", "expectedMs"}


def test_a_warm_provider_gets_no_router_warming_frame(world):
    url, (node,) = world(("ok", {"resident": True}))
    assert "warming" not in [f["type"] for f, _ in _frame_times(url)]


def test_a_replacement_after_a_refusal_says_warming_even_when_the_next_provider_ranks_warm(world):
    url, (busy, ok) = world(("busy", {"in_flight": 0}), ("ok", {"in_flight": 1}))
    types = [f["type"] for f, _ in _frame_times(url)]
    assert types.count("started") == 1 and types.count("warming") == 1
    assert types.index("started") < types.index("warming") < types.index("partial")


def test_a_node_warming_frame_keeps_its_timing_and_loses_everything_else(world):
    url, (node,) = world(("loading", {"resident": True}))
    frames = [f for f, _ in _frame_times(url, wait=2.0, until=("error",))]
    warm = [f for f in frames if f["type"] == "warming"]
    assert warm and warm[-1] == {"type": "warming", "sessionId": "s", "elapsedMs": 900, "expectedMs": 15000}
    assert "ENGINE-ID-7f3a" not in json.dumps(frames)


def _warm_hint(url, **kw):
    import httpx
    base = url.replace("ws://", "http://").rsplit("/ws/", 1)[0]
    return httpx.post(base + "/v1/audio/warm", **kw)


def test_the_warm_hint_starts_a_short_session_on_the_likely_provider_then_lets_go(world, router):
    router._prewarm_last.clear()
    url, (node,) = world(("loading", {"resident": False}))
    r = _warm_hint(url, headers={"authorization": "Bearer x"})
    assert r.status_code == 202 and r.json()["status"] == "accepted" and r.json()["request_id"]
    deadline = time.time() + 8
    while node.closed == 0 and time.time() < deadline:
        time.sleep(0.1)
    assert node.starts == 1 and node.closed == 1          # asked once, then released
    assert json.loads(node.got[0])["sessionId"].startswith("warm-")
    # asked again inside the cooldown: no second session, same opaque answer
    assert _warm_hint(url, headers={"authorization": "Bearer x"}).status_code == 202
    time.sleep(0.5)
    assert node.starts == 1


def test_the_warm_hint_needs_a_credential_and_never_names_the_fleet(world, router):
    router._prewarm_last.clear()
    url, (node,) = world(("ok", {}))
    r = _warm_hint(url)
    assert r.status_code == 401 and node.starts == 0
    ok = _warm_hint(url, headers={"authorization": "Bearer x"})
    assert ok.status_code == 202 and set(ok.json()) == {"status", "request_id"}
