from __future__ import annotations

import base64
import hashlib

from livestack_node.perception.contract import validate_request, validate_result
from livestack_node.perception.glm_ocr import GlmOcrAdapter


def test_glm_uses_shared_contract_and_reports_provenance(tmp_path):
    path = tmp_path / "map.png"
    # The adapter only materializes validated bytes; model-side image decoding is
    # covered by the managed-worker smoke test and does not belong in this unit test.
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="))
    data = path.read_bytes()
    request = validate_request({
        "schemaVersion": "jingway.perception.v1",
        "requestId": "hunyuan-req",
        "images": [{
            "id": "map", "sha256": hashlib.sha256(data).hexdigest(),
            "mediaType": "image/png", "width": 96, "height": 64,
            "contentBase64": base64.b64encode(data).decode(),
            "sourceFromInput": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        }],
        "task": {"type": "text_recognition", "prompt": "literal only"},
        "requirements": {}, "limits": {"maxItems": 8, "maxOutputTokens": 64},
    })
    seen = []
    adapter = GlmOcrAdapter(
        model="zai-org/GLM-OCR", revision="rev",
        generate=lambda image, prompt, limit, control: seen.append((image.exists(), prompt, limit)) or
        ("水師學堂", {"cold_load_ms": 20, "inference_ms": 4, "total_ms": 24,
                    "peak_memory_bytes": 100}),
    )

    result = validate_result(adapter.infer(request, grant={"device": "gpu0"}), request=request)

    assert seen == [(True, "literal only", 64)]
    assert result["observations"][0]["text"] == "水師學堂"
    assert result["observations"][0].get("geometry") is None
    assert result["execution"] == {
        "backend": "cuda", "device": "gpu0", "implementation": "glm-ocr-transformers-v1",
        "model": "zai-org/GLM-OCR", "modelRevision": "rev", "precision": "bf16",
        "preprocessingRevision": "glm-ocr-chat-template-v1", "queueMs": 0.0,
        "coldLoadMs": 20.0, "inferenceMs": 4.0, "totalMs": 24.0,
        "peakMemoryBytes": 100,
    }
