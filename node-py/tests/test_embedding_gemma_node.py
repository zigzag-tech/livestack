"""EmbeddingGemma 2 node (`livestack_node.embedding.serve_gemma`), against the real engine.

A test against a fake engine cannot fail on anything real, so the serving half
needs llama-server and the GGUFs; it is skipped where they are absent. Point it
at them with HARMONY_GEMMA_BIN / _MODEL / _MMPROJ (the same variables the node
reads). The request-validation tests need no engine.
"""
import asyncio
import base64
import io
import os
import struct

import pytest

pytest.importorskip("livestack_node")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from livestack_node.embedding import serve_gemma  # noqa: E402

MODEL = "google/embeddinggemma-2"


def run(coro):
    return asyncio.run(coro)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=300)


def data_uri(color, label):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    im = Image.new("RGB", (512, 384), color)
    ImageDraw.Draw(im).text((120, 180), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def image_item(uri):
    return {"content": [{"type": "image_url", "image_url": {"url": uri}}]}


def floats(b64):
    raw = base64.b64decode(b64)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)


# --- validation: no engine needed --------------------------------------------

def test_remote_image_urls_are_refused_not_fetched():
    # llama-server would fetch an http(s) URL itself: that is an SSRF proxy.
    with pytest.raises(serve_gemma.BadRequest, match="data:image/"):
        serve_gemma.normalise_inputs([image_item("http://169.254.169.254/latest/meta-data")])
    with pytest.raises(serve_gemma.BadRequest, match="data:image/"):
        serve_gemma.normalise_inputs([image_item("file:///etc/passwd")])


def test_unknown_parts_and_oversize_are_named():
    with pytest.raises(serve_gemma.BadRequest, match="audio"):
        serve_gemma.normalise_inputs([{"content": [{"type": "audio", "audio": "x"}]}])
    with pytest.raises(serve_gemma.BadRequest, match="at most"):
        serve_gemma.normalise_inputs(["x"] * (serve_gemma.MAX_INPUTS + 1))
    with pytest.raises(serve_gemma.BadRequest):
        serve_gemma.normalise_inputs([])


def test_lane_defaults_media_and_big_batches_to_bulk():
    assert serve_gemma.pick_lane(None, ["q"], False) == "query"
    assert serve_gemma.pick_lane(None, ["q"], True) == "bulk"
    assert serve_gemma.pick_lane(None, ["q"] * 9, False) == "bulk"
    assert serve_gemma.pick_lane("bulk", ["q"], False) == "bulk"
    with pytest.raises(serve_gemma.BadRequest):
        serve_gemma.pick_lane("urgent", ["q"], False)


def test_truncation_renormalises():
    import numpy as np
    out = serve_gemma.shape_vectors([[3.0, 4.0, 12.0, 0.0]], 2)
    assert np.allclose(out, [[0.6, 0.8]])


# --- the node, against the real engine ---------------------------------------

@pytest.fixture(scope="module")
def node():
    for var in ("HARMONY_GEMMA_BIN", "HARMONY_GEMMA_MODEL", "HARMONY_GEMMA_MMPROJ"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set: no real EmbeddingGemma engine on this host")
    os.environ.setdefault("LIVESTACK_REGISTER", "0")
    os.environ.setdefault("LIVESTACK_MACHINE_ID", "box")
    os.environ.setdefault("HARMONY_GEMMA_UPSTREAM_PORT", "8251")
    app = serve_gemma.create_app(port=None)
    yield app
    app.state.embed_manager.unload_now()


def post(app, body):
    async def go():
        async with client_for(app) as c:
            return await c.post("/v1/embeddings", json={"model": MODEL, **body})
    return run(go())


def test_text_round_trip_in_both_encodings_and_dimensions(node):
    texts = ["杭州参赛", "fix the hub OOM caused by the embedder"]
    f = post(node, {"input": texts}).json()
    b = post(node, {"input": texts, "encoding_format": "base64"}).json()
    assert len(f["data"]) == 2 and len(f["data"][0]["embedding"]) == 512
    assert max(abs(x - y) for x, y in zip(f["data"][0]["embedding"], floats(b["data"][0]["embedding"]))) < 1e-5
    assert abs(sum(x * x for x in f["data"][0]["embedding"]) - 1.0) < 1e-4
    d256 = post(node, {"input": texts[0], "dimensions": 256}).json()
    assert len(d256["data"][0]["embedding"]) == 256
    assert post(node, {"input": texts[0], "dimensions": 100}).status_code == 400


def test_images_change_the_vector_and_missing_media_is_named(node):
    red, blue = data_uri((200, 30, 30), "RED CAT"), data_uri((30, 30, 200), "BLUE DOG")
    vec = lambda uri: post(node, {"input": [image_item(uri)]}).json()["data"][0]["embedding"]
    a, a_again, b = vec(red), vec(red), vec(blue)
    assert cosine(a, a_again) > 0.999
    assert cosine(a, b) < 0.95                    # measured 0.86 on 2026-10-12
    gone = post(node, {"input": [image_item("data:image/jpeg;base64,")]})
    assert gone.status_code == 400 and "image" in gone.json()["error"]["message"]


def test_full_bulk_lane_sheds_while_query_is_served(node, monkeypatch):
    monkeypatch.setitem(serve_gemma.LANE_LIMITS, "bulk", 0)
    shed = post(node, {"input": [image_item(data_uri((1, 2, 3), "x"))]})
    assert shed.status_code == 503 and "bulk lane full" in shed.json()["error"]["message"]
    assert post(node, {"input": "still served"}).status_code == 200


def test_long_chinese_input_is_not_rejected_by_the_batch_size(node):
    # The engine default (-ub 512) rejected a ~1000-token chunk with HTTP 500.
    r = post(node, {"input": "会议讨论了模型部署和显存分配的问题。" * 120})
    assert r.status_code == 200, r.text


def test_vulkan_device_id_is_under_the_machine_not_the_host_id(monkeypatch):
    # The hub's roster producer drops a node of another host id unless its
    # device id starts with `{machine}/`; `{host_id}/vk1` made the node invisible.
    # Needs no engine: create_app only checks that the files exist.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for name in ("bin", "model", "mmproj"):
            open(os.path.join(d, name), "w").close()
        monkeypatch.setenv("HARMONY_GEMMA_BIN", os.path.join(d, "bin"))
        monkeypatch.setenv("HARMONY_GEMMA_MODEL", os.path.join(d, "model"))
        monkeypatch.setenv("HARMONY_GEMMA_MMPROJ", os.path.join(d, "mmproj"))
        monkeypatch.setenv("HARMONY_GEMMA_BACKEND", "vulkan")
        monkeypatch.setenv("HARMONY_GEMMA_VK_DEVICE", "1")
        monkeypatch.setenv("HARMONY_GEMMA_HOST_ID", "box-gemma")
        monkeypatch.setenv("LIVESTACK_REGISTER", "0")
        monkeypatch.setenv("LIVESTACK_MACHINE_ID", "box")
        monkeypatch.delenv("LIVESTACK_DEVICE_ID", raising=False)
        app = serve_gemma.create_app(port=None)

        async def go():
            async with client_for(app) as c:
                return (await c.get("/livestack/capability")).json()

        assert run(go())["device_id"] == "box/vk1"
