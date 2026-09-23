from __future__ import annotations

import base64, hashlib
from livestack_node.perception.contract import validate_request, validate_result
from livestack_node.perception.paddleocr import PaddleOcrAdapter


def test_ppocr_returns_source_polygons_and_partial_on_cap():
    data=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    image={"id":"map","sha256":hashlib.sha256(data).hexdigest(),"mediaType":"image/png","width":100,"height":80,"contentBase64":base64.b64encode(data).decode(),"sourceFromInput":[1,0,0,0,1,0,0,0,1]}
    request=validate_request({"schemaVersion":"jingway.perception.v1","requestId":"p","images":[image],"task":{"type":"text_detection","includeUnreadable":True},"requirements":{},"limits":{"maxItems":1}})
    rows=[{"text":"甲","score":.9,"polygon":[[1,2],[3,2],[3,4],[1,4]]},{"text":"乙","score":.8,"polygon":[[5,6],[7,6],[7,8],[5,8]]}]
    adapter=PaddleOcrAdapter(model="m",revision="r",run=lambda path,control:(rows,{"inference_ms":1,"total_ms":1}))
    result=validate_result(adapter.infer(request,grant={}),request=request)
    assert result["outcome"] == "partial" and result["truncated"] is True
    assert result["observations"][0]["text"] == "甲"
    assert result["observations"][0]["geometry"]["polygon"]["exterior"][2] == {"x":3.0,"y":4.0}
