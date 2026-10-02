"""CPU embedding node: `attach(backend="cpu")` and `livestack_node.embedding.serve`.

The attach half runs anywhere the facade deps are installed. The serving half
needs the real ONNX exports (rule: a test against a fake runtime cannot fail on
anything real) and is skipped where they are absent; on an embedding host it
finds them through HARMONY_EMBED_TEST_MODEL_DIRS or the local Hugging Face cache.
"""
import asyncio
import base64
import glob
import os
import struct

import pytest

pytest.importorskip("livestack_node")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from livestack_node import ManagedUnit, attach, noop_free  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_cpu_backend_is_stamped_and_takes_no_card(monkeypatch):
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_MACHINE_ID", "box")
    monkeypatch.delenv("LIVESTACK_DEVICE_ID", raising=False)
    # A declared backend that disagrees with where the node runs is the lying
    # attribute; the node's own statement overwrites it.
    unit = ManagedUnit("e", loader=lambda: "m", freer=noop_free,
                       attributes={"class": "embed", "backend": "cuda"})
    app = FastAPI()
    attach(app, host_id="box-embed", kind="embed", units={"e": unit}, idle_seconds=60,
           coload=True, gpu_call=lambda fn: fn(), backend="cpu")

    async def go():
        async with client_for(app) as c:
            return ((await c.get("/livestack/capability")).json(),
                    (await c.get("/livestack/residence")).json())

    cap, res = run(go())
    assert cap["device_id"] == "box/cpu"
    assert cap["device_candidates"] == ["box/cpu"]
    assert res["device_id"] == "box/cpu"
    assert res["units"][0]["attributes"] == {"class": "embed", "backend": "cpu"}
    # The device meter is host RAM, so the planner budgets the node against
    # memory it actually has rather than a default card size.
    mem = res["device_mem"]
    assert mem["capacity"]["vram_bytes"] > 0
    assert 0 <= mem["free"]["vram_bytes"] <= mem["capacity"]["vram_bytes"]


def test_no_backend_keeps_the_old_identity(monkeypatch):
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_DEVICE_ID", "box/abcd1234")
    unit = ManagedUnit("e", loader=lambda: "m", freer=noop_free, attributes={"class": "embed"})
    app = FastAPI()
    attach(app, host_id="box", kind="embed", units={"e": unit}, idle_seconds=60,
           coload=True, gpu_call=lambda fn: fn())
    assert unit.attributes == {"class": "embed"}

    async def go():
        async with client_for(app) as c:
            return (await c.get("/livestack/capability")).json()

    assert run(go())["device_id"] == "box/abcd1234"


# --- the serving node, against real model files -----------------------------

MODELS = {
    "Xenova/all-MiniLM-L6-v2": "models--Xenova--all-MiniLM-L6-v2",
    "Xenova/paraphrase-multilingual-MiniLM-L12-v2": "models--Xenova--paraphrase-multilingual-MiniLM-L12-v2",
}


def _local_model_dirs():
    explicit = os.environ.get("HARMONY_EMBED_TEST_MODEL_DIRS")
    if explicit:                       # "id=dir,id=dir"
        return dict(pair.split("=", 1) for pair in explicit.split(",") if "=" in pair)
    roots = [os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"),
             os.path.expanduser("~/harmony-embed/hf-cache")]
    out = {}
    for model_id, folder in MODELS.items():
        for root in roots:
            hits = glob.glob(os.path.join(root, "hub", folder, "snapshots", "*",
                                          "onnx", "model_quantized.onnx"))
            if hits:
                out[model_id] = os.path.dirname(os.path.dirname(hits[0]))
                break
    return out


def _node(monkeypatch):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    dirs = _local_model_dirs()
    if set(dirs) != set(MODELS):
        pytest.skip("real q8 ONNX exports not on this host")
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_MACHINE_ID", "box")
    monkeypatch.setenv("HARMONY_EMBED_THREADS", "2")
    from livestack_node.embedding.serve import create_app
    return create_app(list(MODELS), model_dirs=dirs, port=None)


def _floats(b64):
    raw = base64.b64decode(b64)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def test_embeddings_round_trip_in_both_encodings(monkeypatch):
    app = _node(monkeypatch)
    texts = ["杭州参赛", "fix the hub OOM caused by the embedder", ""]
    multi = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"

    async def go():
        async with client_for(app) as c:
            f = await c.post("/v1/embeddings", json={"model": multi, "input": texts})
            b = await c.post("/v1/embeddings", json={"model": multi, "input": texts,
                                                     "encoding_format": "base64"})
            one = await c.post("/v1/embeddings", json={"model": multi, "input": texts[0],
                                                       "encoding_format": "base64"})
            return f, b, one

    f, b, one = run(go())
    assert f.status_code == 200 and b.status_code == 200
    assert f.json()["model"] == multi
    floats = [d["embedding"] for d in f.json()["data"]]
    decoded = [_floats(d["embedding"]) for d in b.json()["data"]]
    assert [len(v) for v in floats] == [384, 384, 384]
    for v, w in zip(floats, decoded):
        assert max(abs(x - y) for x, y in zip(v, w)) < 1e-6
        assert abs(sum(x * x for x in v) - 1.0) < 1e-4          # L2-normalised
    # A text's vector does not depend on what it was sent with (q8 quantizes
    # activations per tensor, so a batched inference would make it depend).
    alone = _floats(one.json()["data"][0]["embedding"])
    assert alone == decoded[0]
    # base64 float32 is several times smaller than JSON decimals.
    assert len(b.content) * 3 < len(f.content)


def test_bounds_unknown_models_and_readiness(monkeypatch):
    app = _node(monkeypatch)

    async def go():
        async with client_for(app) as c:
            unknown = await c.post("/v1/embeddings", json={"model": "nope", "input": "x"})
            big = await c.post("/v1/embeddings", json={
                "model": "Xenova/all-MiniLM-L6-v2", "input": ["x"] * 257})
            bad = await c.post("/v1/embeddings", json={
                "model": "Xenova/all-MiniLM-L6-v2", "input": [1, 2]})
            cap = (await c.get("/livestack/capability")).json()
            res = (await c.get("/livestack/residence")).json()
            return unknown, big, bad, cap, res

    unknown, big, bad, cap, res = run(go())
    assert unknown.status_code == 404 and "served" in unknown.json()["error"]["message"]
    assert big.status_code == 413
    assert bad.status_code == 400
    # Ready means "can serve", which an unloaded unit can — on demand. A node
    # that reported not-ready after an idle unload would never be routed to
    # again, and so never load again.
    assert cap["ready"] is True
    attrs = {u["kind"]: u["attributes"] for u in res["units"]}
    assert attrs["embed_paraphrase_multilingual_minilm_l12_v2"] == {
        "class": "embed", "model": "Xenova/paraphrase-multilingual-MiniLM-L12-v2",
        "quant": "q8", "dim": 384, "backend": "cpu"}


def test_an_unloaded_model_comes_back_with_the_same_vectors(monkeypatch):
    app = _node(monkeypatch)
    manager = app.state.embed_manager
    body = {"model": "Xenova/all-MiniLM-L6-v2", "input": ["same text"], "encoding_format": "base64"}

    async def post():
        async with client_for(app) as c:
            return (await c.post("/v1/embeddings", json=body)).json()["data"][0]["embedding"]

    before = run(post())
    # Let the startup preload finish, so the unload below is not raced by it.
    import time
    deadline = time.monotonic() + 30
    while not all(u.loaded for u in manager.units.values()) and time.monotonic() < deadline:
        time.sleep(0.1)
    manager.unload_now()
    assert not any(u.loaded for u in manager.units.values())
    assert run(post()) == before
