from __future__ import annotations

import base64
import hashlib

from livestack_node.perception.contract import validate_request, validate_result
from livestack_node.perception.deepseek_ocr2 import DeepSeekOCR2Adapter


def request(tmp_path, *, regions=None):
    from PIL import Image
    path = tmp_path / "map.png"
    Image.new("RGB", (100, 80), "white").save(path)
    data = path.read_bytes()
    task = {"type": "text_recognition", "prompt": "<image>\nFree OCR. "}
    if regions is not None:
        task["regions"] = regions
    return validate_request({
        "schemaVersion": "jingway.perception.v1", "requestId": "ocr-req",
        "images": [{"id": "map", "sha256": hashlib.sha256(data).hexdigest(),
                    "mediaType": "image/png", "width": 100, "height": 80,
                    "contentBase64": base64.b64encode(data).decode(),
                    "sourceFromInput": [1, 0, 0, 0, 1, 0, 0, 0, 1]}],
        "task": task, "requirements": {},
        "limits": {"maxItems": 8, "maxOutputTokens": 64},
    })


def test_whole_image_recognition_uses_the_shared_contract(tmp_path):
    seen = []
    adapter = DeepSeekOCR2Adapter(
        model="deepseek-ai/DeepSeek-OCR-2", revision="rev",
        generate=lambda path, prompt, limit, control: seen.append((path.exists(), prompt, limit)) or
            ("鐵道部", {"inference_ms": 12, "total_ms": 12, "peak_memory_bytes": 99}),
    )
    req = request(tmp_path)
    result = validate_result(adapter.infer(req, grant={"device": "gpu0"}), request=req)
    assert result["observations"][0]["text"] == "鐵道部"
    assert result["observations"][0].get("geometry") is None
    assert result["execution"]["backend"] == "cuda"
    assert seen == [(True, "<image>\nFree OCR. ", 64)]


def test_region_recognition_preserves_tool_geometry(tmp_path):
    polygon = {"exterior": [{"x": 10, "y": 10}, {"x": 60, "y": 10},
                              {"x": 60, "y": 50}, {"x": 10, "y": 50}], "holes": []}
    sizes = []

    def generate(path, _prompt, _limit, _control):
        from PIL import Image
        with Image.open(path) as image:
            sizes.append(image.size)
        return "公園", {"inference_ms": 1, "total_ms": 1}

    adapter = DeepSeekOCR2Adapter(model="m", revision="r", generate=generate)
    req = request(tmp_path, regions=[{"id": "region-1", "polygon": polygon}])
    result = validate_result(adapter.infer(req, grant={"device": "gpu0"}), request=req)
    assert sizes == [(50, 40)]
    assert result["observations"][0]["queryId"] == "region-1"
    assert result["observations"][0]["geometry"] == {"kind": "polygon", "polygon": polygon}
