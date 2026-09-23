from __future__ import annotations

import base64
import hashlib

import pytest

from livestack_node.perception.contract import PerceptionContractError, validate_request, validate_result
from livestack_node.perception.paddleocr_vl import PaddleOcrVlAdapter, parse_spotting


def image():
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII=")
    return data, {"id":"map","sha256":hashlib.sha256(data).hexdigest(),"mediaType":"image/png",
                  "width":200,"height":100,"contentBase64":base64.b64encode(data).decode(),
                  "sourceFromInput":[1,0,0,0,1,0,0,0,1]}


def test_spotting_parser_scales_each_axis_to_source_pixels():
    _data, img = image()
    rows, truncated = parse_spotting(
        "水師學堂<|LOC_100|><|LOC_200|><|LOC_400|><|LOC_200|><|LOC_400|><|LOC_800|><|LOC_100|><|LOC_800|>",
        image=img, max_items=8)
    assert truncated is False
    assert rows[0]["text"] == "水師學堂"
    assert rows[0]["geometry"]["polygon"]["exterior"] == [
        {"x":20.0,"y":20.0},{"x":80.0,"y":20.0},{"x":80.0,"y":80.0},{"x":20.0,"y":80.0}]


def test_detection_and_recognition_share_contract():
    _data, img = image(); calls=[]
    adapter=PaddleOcrVlAdapter(model="PaddlePaddle/PaddleOCR-VL-1.6",revision="rev",
        generate=lambda path,prompt,limit,control: calls.append((prompt,limit)) or
        (("公園<|LOC_10|><|LOC_20|><|LOC_30|><|LOC_20|><|LOC_30|><|LOC_40|><|LOC_10|><|LOC_40|>" if prompt=="Spotting:" else "公園"),
         {"inference_ms":4,"total_ms":4,"peak_memory_bytes":10}))
    detection=validate_request({"schemaVersion":"jingway.perception.v1","requestId":"detect","images":[img],
        "task":{"type":"text_detection","includeUnreadable":True},"requirements":{},"limits":{"maxItems":8,"maxOutputTokens":64}})
    result=validate_result(adapter.infer(detection,grant={"device":"gpu0"}),request=detection)
    assert result["observations"][0]["geometry"]["kind"] == "polygon"
    recognition=validate_request({"schemaVersion":"jingway.perception.v1","requestId":"read","images":[img],
        "task":{"type":"text_recognition","prompt":"OCR:"},"requirements":{},"limits":{"maxItems":8,"maxOutputTokens":64}})
    result=validate_result(adapter.infer(recognition,grant={"device":"gpu0"}),request=recognition)
    assert result["observations"][0]["text"] == "公園"
    assert calls == [("Spotting:",64),("OCR:",64)]


def test_generation_or_item_cap_is_reported_as_partial():
    _data, img = image()
    raw = "\n".join([
        "甲<|LOC_10|><|LOC_10|><|LOC_20|><|LOC_10|><|LOC_20|><|LOC_20|><|LOC_10|><|LOC_20|>",
        "乙<|LOC_30|><|LOC_30|><|LOC_40|><|LOC_30|><|LOC_40|><|LOC_40|><|LOC_30|><|LOC_40|>",
    ])
    adapter = PaddleOcrVlAdapter(
        model="m", revision="r",
        generate=lambda *_: (raw, {"inference_ms": 1, "total_ms": 1,
                                   "peak_memory_bytes": 1, "truncated": True}))
    request = validate_request({
        "schemaVersion":"jingway.perception.v1", "requestId":"cap", "images":[img],
        "task":{"type":"text_detection","includeUnreadable":True}, "requirements":{},
        "limits":{"maxItems":1,"maxOutputTokens":64}})
    result = validate_result(adapter.infer(request, grant={}), request=request)
    assert result["outcome"] == "partial"
    assert result["truncated"] is True
    assert len(result["observations"]) == 1


def test_region_recognition_is_rejected_instead_of_ignoring_geometry():
    _data, img=image(); adapter=PaddleOcrVlAdapter(model="m",revision="r",generate=lambda *_: ("",{}))
    polygon={"exterior":[{"x":0,"y":0},{"x":10,"y":0},{"x":10,"y":10}],"holes":[]}
    request=validate_request({"schemaVersion":"jingway.perception.v1","requestId":"r","images":[img],
        "task":{"type":"text_recognition","regions":[{"id":"p","polygon":polygon}]},
        "requirements":{},"limits":{"maxItems":8}})
    with pytest.raises(PerceptionContractError) as caught: adapter.infer(request,grant={})
    assert caught.value.cause == "unsupported"
