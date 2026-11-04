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
        self.got, self.mode, self.starts = [], mode, 0

        async def handler(ws):
            async for m in ws:
                self.got.append(m)
                if isinstance(m, str) and json.loads(m).get("type") == "start":
                    self.starts += 1
                    await ws.send(json.dumps({"type": "started", "protocol": 1, "sessionId": "s", "ackSeq": -1}))
                    if self.mode == "busy":
                        await ws.send(json.dumps({"type": "error", "error": "Harmony did not grant this device: held by image"}))
                        await ws.close(code=1013, reason="busy")
                        return
                elif isinstance(m, bytes) and self.mode == "ok":
                    await ws.send(json.dumps({"type": "partial", "text": "hi", "from": self.port}))

        self.srv = lp.run(self._start(handler))
        self.port = self.srv.sockets[0].getsockname()[1]

    async def _start(self, handler):
        return await serve(handler, "127.0.0.1", 0)

    def row(self, streaming="native", in_flight=0):
        return {"peer": f"http://127.0.0.1:{self.port}/livestack", "state": "fresh", "ready": True,
                "kinds": ["polyasr"], "region": "na", "detail": "x", "load": {"in_flight": in_flight},
                "inventory": {"asr": {"streaming": streaming}},
                "units": [{"kind": "asr", "resident": True}]}


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


async def _dictate(url, audio=b"\x00" * 64, wait=3.0):
    out = []
    async with connect(url) as ws:
        await ws.send(START)
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


def test_every_provider_refusing_ends_in_an_error_frame_naming_each_try(world):
    url, nodes = world(("busy", {"in_flight": 0}), ("busy", {"in_flight": 1}))
    frames = Loop().run(_dictate(url))
    err = [f for f in frames if f["type"] == "error"][-1]
    assert "did not grant" in err["error"] and len(err["attempts"]) == 2 and err["retryAfterS"] == 30
    assert [n.starts for n in nodes] == [1, 1]


def test_native_streaming_is_a_preference_not_a_requirement(world):
    # equal distance and load: native wins the tie ...
    url, (syn, nat) = world(("ok", {"streaming": "synthetic"}), ("ok", {"streaming": "native"}))
    frames = Loop().run(_dictate(url))
    assert [f for f in frames if f["type"] == "partial"][0]["from"] == nat.port
    # ... and a synthetic-only fleet still serves
    url, (only,) = world(("ok", {"streaming": "synthetic"}))
    assert [f for f in Loop().run(_dictate(url)) if f["type"] == "partial"][0]["from"] == only.port
