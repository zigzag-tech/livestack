import hashlib

import numpy as np
import pytest

from livestack_node.perception.contract import PerceptionContractError
from livestack_node.perception.sam21 import Sam21Adapter


def request(tmp_path, prompts, returns=("polygon",)):
    path = tmp_path / "image.png"; path.write_bytes(b"image")
    return {"schemaVersion":"jingway.perception.v1","requestId":"sam-1",
            "images":[{"id":"image","sha256":hashlib.sha256(b"image").hexdigest(),"mediaType":"image/png",
                       "width":20,"height":10,"objectRef":str(path),"sourceFromInput":[1,0,0,0,1,0,0,0,1]}],
            "task":{"type":"segmentation","prompts":prompts,"return":list(returns)},
            "requirements":{},"limits":{"maxItems":100}}


def segment(_path, groups):
    masks=[]
    for _ in groups:
        mask=np.zeros((10,20),bool);mask[2:8,4:16]=True;mask[4:6,8:12]=False;masks.append(mask)
    return masks,{"cold_load_ms":3,"inference_ms":4,"total_ms":7,"peak_memory_bytes":99}


def test_box_prompts_map_to_source_polygons_and_boxes(tmp_path):
    prompts=[{"type":"box","id":"a","box":{"xMin":3,"yMin":1,"xMax":17,"yMax":9}},
             {"type":"box","id":"b","box":{"xMin":2,"yMin":1,"xMax":18,"yMax":9}}]
    got=Sam21Adapter(model="sam",revision="r",segment=segment).infer(request(tmp_path,prompts,("polygon","box")),grant={})
    assert got["outcome"]=="ok" and len(got["observations"])==4
    assert {row["queryId"] for row in got["observations"]}=={"a","b"}
    polygons=[row for row in got["observations"] if row["geometry"]["kind"]=="polygon"]
    assert all(len(row["geometry"]["polygon"]["holes"])==1 for row in polygons)
    assert got["execution"]["inferenceMs"]==4


def test_positive_and_negative_points_are_one_interactive_group(tmp_path):
    prompts=[{"type":"point","id":"positive","point":{"x":5,"y":5},"label":"positive"},
             {"type":"point","id":"negative","point":{"x":1,"y":1},"label":"negative"}]
    got=Sam21Adapter(model="sam",revision="r",segment=segment).infer(request(tmp_path,prompts),grant={})
    assert got["observations"][0]["queryId"]=="positive"
    assert got["observations"][0]["attributes"]["promptIds"]==["positive","negative"]


@pytest.mark.parametrize("prompts,returns", [
    ([{"type":"point","id":"n","point":{"x":1,"y":1},"label":"negative"}], ("polygon",)),
    ([{"type":"text","id":"t","text":"road"}], ("polygon",)),
    ([{"type":"box","id":"b","box":{"xMin":1,"yMin":1,"xMax":2,"yMax":2}}], ("mask",)),
])
def test_unsupported_or_ambiguous_requests_fail(tmp_path,prompts,returns):
    with pytest.raises(PerceptionContractError):
        Sam21Adapter(model="sam",revision="r",segment=segment).infer(request(tmp_path,prompts,returns),grant={})
