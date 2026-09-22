from __future__ import annotations

import hashlib
import base64
from types import SimpleNamespace

from livestack_node.perception.contract import validate_request, validate_result
from livestack_node.perception.locateanything import (
    CudaLocateAnythingRuntime, LocateAnythingAdapter, MlxLocateAnythingRuntime,
    parse_grounding,
)


def image(tmp_path):
    path = tmp_path / "map.webp"
    path.write_bytes(b"fixture-image")
    return path, {"id": "map", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "mediaType": "image/webp", "width": 200, "height": 100,
                  "objectRef": str(path), "sourceFromInput": [1, 0, 0, 0, 1, 0, 0, 0, 1]}


def test_parser_maps_model_coordinates_to_source_pixels_and_caps(tmp_path):
    _, img = image(tmp_path)
    raw = "<ref>building</ref><box><100><200><400><800></box><box><500><100><900><300></box><|im_end|>"
    rows, truncated, missing = parse_grounding(raw, image=img,
        queries=[{"id": "q1", "text": "building"}], max_items=1)
    assert rows[0]["geometry"]["box"] == {"xMin": 20, "yMin": 20, "xMax": 80, "yMax": 80}
    assert truncated is True
    assert missing == []


def test_cuda_and_mlx_share_a_validated_result_contract(tmp_path):
    _path, img = image(tmp_path)
    req = validate_request({
        "schemaVersion": "jingway.perception.v1", "requestId": "req", "images": [img],
        "task": {"type": "grounding", "queryMode": "description",
                 "queries": [{"id": "q1", "text": "closed pink region"}], "geometry": ["box"]},
        "requirements": {}, "limits": {"maxItems": 8, "maxOutputTokens": 64},
    })
    for backend in ("cuda", "mlx"):
        calls = []
        adapter = LocateAnythingAdapter(
            backend=backend, device="device", implementation=f"locateanything-{backend}-v1",
            model="LocateAnything-3B", model_revision="c32291c", precision="bf16",
            preprocessing_revision="upstream-c32291c",
            generate=lambda path, prompt, limit, control: calls.append((path, prompt, limit)) or
                ("<ref>closed pink region</ref><box><10><20><200><300></box><|im_end|>", {"inference_ms": 5}),
        )
        result = validate_result(adapter.infer(req, grant={"device": "placed", "queue_ms": 2}), request=req)
        assert result["execution"]["backend"] == backend
        assert result["execution"]["device"] == "placed"
        assert calls[0][1:] == ("closed pink region", 64)


def test_inline_image_bytes_are_materialized_and_hash_checked(tmp_path):
    data = b"inline-webp-fixture"
    img = {"id": "map", "sha256": hashlib.sha256(data).hexdigest(),
           "mediaType": "image/webp", "width": 200, "height": 100,
           "contentBase64": base64.b64encode(data).decode(),
           "sourceFromInput": [1, 0, 0, 0, 1, 0, 0, 0, 1]}
    req = validate_request({
        "schemaVersion": "jingway.perception.v1", "requestId": "inline", "images": [img],
        "task": {"type": "grounding", "queryMode": "category",
                 "queries": [{"id": "q", "text": "building"}], "geometry": ["box"]},
        "requirements": {}, "limits": {"maxItems": 4, "maxOutputTokens": 16},
    })
    seen = []
    adapter = LocateAnythingAdapter(
        backend="mlx", device="mlx0", implementation="test", model="m",
        model_revision="r", precision="int8", preprocessing_revision="p",
        generate=lambda path, prompt, limit, control: seen.append((path.exists(), path.read_bytes())) or
            ("<ref>building</ref><box><1><2><3><4></box>", {}),
    )
    result = adapter.infer(req, grant={"device": "mlx0"})
    assert result["outcome"] == "ok"
    assert seen == [(True, data)]


def test_none_is_an_explicit_empty_result(tmp_path):
    _path, img = image(tmp_path)
    rows, truncated, missing = parse_grounding("<ref>pond</ref><box>None</box><|im_end|>",
        image=img, queries=[{"id": "pond", "text": "pond"}], max_items=4)
    assert rows == []
    assert truncated is False
    assert missing == []


def test_cuda_close_clears_upstream_global_model_cache():
    upstream = SimpleNamespace(_model=object(), _tok=object(), _proc=object())
    runtime = CudaLocateAnythingRuntime()
    runtime._runtime = upstream
    runtime._model = upstream._model
    runtime._tokenizer = upstream._tok
    runtime._load_ms = 123

    runtime.close()

    assert runtime._runtime is None
    assert runtime._model is None
    assert runtime._tokenizer is None
    assert runtime._load_ms == 0
    assert upstream._model is None
    assert upstream._tok is None
    assert upstream._proc is None


def test_mlx_close_stops_the_owned_worker_process():
    class Connection:
        def __init__(self):
            self.messages = []
            self.closed = False
        def send(self, message):
            self.messages.append(message)
        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.joins = []
            self.terminated = False
        def join(self, timeout):
            self.joins.append(timeout)
        def is_alive(self):
            return False
        def terminate(self):
            self.terminated = True

    runtime = MlxLocateAnythingRuntime()
    runtime._connection = connection = Connection()
    runtime._process = process = Process()
    runtime._load_ms = 123

    runtime.close()

    assert connection.messages == [{"op": "close"}]
    assert connection.closed is True
    assert process.joins == [10]
    assert process.terminated is False
    assert runtime._connection is None and runtime._process is None
    assert runtime._load_ms == 0
