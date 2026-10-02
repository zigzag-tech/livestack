import pytest

from livestack_node.imagegen.contract import resolve_worker, validate_request
from livestack_node.planner import Unit, Request, WorldState, candidate_kinds


def units():
    return {
        "qwen_image_2512": Unit(kind="qwen_image_2512", footprint={}, attributes={"class": "imagegen", "task": "text_to_image", "params_b": 20}),
        "z_image_turbo": Unit(kind="z_image_turbo", footprint={}, attributes={"class": "imagegen", "task": "text_to_image", "params_b": 6}),
    }


@pytest.mark.parametrize("constraint,expected", [({"params_b>=": 20}, "qwen_image_2512"), ({"params_b<=": 6}, "z_image_turbo")])
def test_same_prompt_routes_by_requirements(constraint, expected):
    body = validate_request({"prompt": "A red bicycle in the rain", "harmony_requires": constraint})
    candidates = candidate_kinds(WorldState(units=units(), devices=()), Request(id="pair", kind="", requires=body["harmony_requires"]))
    assert candidates == [expected]


def test_misleading_or_degraded_grant_cannot_silently_substitute_model():
    view = {"hosts": {"joe": {"nodes": [{"state": "fresh", "device_id": "joe/gpu", "peer": "http://joe:8210/livestack", "units": [{"kind": "z_image_turbo", "attributes": units()["z_image_turbo"].attributes}]}]}}}
    with pytest.raises(RuntimeError):
        resolve_worker(view, {"granted": True, "kind": "z_image_turbo", "device_id": "joe/gpu"}, {"class": "imagegen", "params_b>=": 20})
    with pytest.raises(RuntimeError):
        resolve_worker(view, {"granted": True, "degraded": "broker down"}, {})


@pytest.mark.parametrize("body", [{"prompt": ""}, {"prompt": "x", "width": 4096}, {"prompt": "x", "seed": -1}, {"prompt": "x", "harmony_requires": {"class": "llm"}}])
def test_invalid_requests_fail_before_inference(body):
    with pytest.raises(ValueError):
        validate_request(body)
