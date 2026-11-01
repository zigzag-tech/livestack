import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from livestack_node.imagegen.contract import (
    MAX_CONTROL_IMAGE_BYTES, resolve_worker, satisfies, validate_request,
)
from livestack_node.planner import Unit, Request, WorldState, candidate_kinds


BASE_REVISION = "1" * 40
CONTROLNET_REVISION = "2" * 40
CONTROLNET_CONFIG_SHA256 = "3" * 64


def _png_bytes(width=768, height=432, **save_kwargs):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (48, 96, 128)).save(
        stream, format="PNG", **save_kwargs)
    return stream.getvalue()


def _control_image(data=None):
    data = _png_bytes() if data is None else data
    return {
        "media_type": "image/png",
        "b64_json": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _guided_request():
    return {
        "prompt": "A grounded landscape",
        "width": 768,
        "height": 432,
        "seed": 1863,
        "control_image": _control_image(),
        "controlnet_conditioning_scale": 0.8,
        "harmony_requires": {
            "class": "imagegen",
            "task": "text_to_image",
            "depth_guide": True,
            "model": "Qwen/Qwen-Image-2512",
            "revision": BASE_REVISION,
            "controlnet_model": "test/Qwen-ControlNet",
            "controlnet_revision": CONTROLNET_REVISION,
            "controlnet_config_sha256": CONTROLNET_CONFIG_SHA256,
            "quantization": "nf4",
        },
    }


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


def test_qwen_native_wide_dimensions_are_accepted():
    request = validate_request({"prompt": "native quality", "width": 1664, "height": 928})
    assert (request["width"], request["height"]) == (1664, 928)


def test_unguided_legacy_request_does_not_require_revision_or_depth_fields():
    request = validate_request({"prompt": "ordinary text to image"})
    assert request["harmony_requires"] == {
        "class": "imagegen", "task": "text_to_image",
    }
    assert "control_image" not in request
    assert "controlnet_conditioning_scale" not in request


@pytest.mark.parametrize("scale", [0.8, 1, 1.0])
def test_valid_depth_request_preserves_json_for_forwarding(scale):
    body = _guided_request()
    body["controlnet_conditioning_scale"] = scale
    request = validate_request(body)
    assert request["control_image"] == body["control_image"]
    assert isinstance(request["control_image"]["b64_json"], str)
    assert request["controlnet_conditioning_scale"] == scale
    assert request["harmony_requires"] == body["harmony_requires"]
    assert (request["width"], request["height"], request["seed"]) == (768, 432, 1863)
    json.dumps(request, allow_nan=False)


@pytest.mark.parametrize("scale", [
    True, False, None, "0.8", 0.799, 1.001, 10**1000,
    float("nan"), float("inf"), float("-inf"),
])
def test_depth_scale_is_non_boolean_finite_and_bounded(scale):
    body = _guided_request()
    body["controlnet_conditioning_scale"] = scale
    with pytest.raises(ValueError, match="scale"):
        validate_request(body)


@pytest.mark.parametrize("value", [1, 0, False, None, "true"])
def test_depth_requirement_is_literal_json_true(value):
    body = _guided_request()
    body["harmony_requires"]["depth_guide"] = value
    with pytest.raises(ValueError, match="literal JSON true"):
        validate_request(body)


@pytest.mark.parametrize("key,value", [
    ("task", "image_to_image"), ("task", None), ("class", "llm"),
])
def test_guided_task_and_class_are_exact(key, value):
    body = _guided_request()
    body["harmony_requires"][key] = value
    with pytest.raises(ValueError):
        validate_request(body)


def test_unguided_wrong_task_is_also_refused():
    with pytest.raises(ValueError, match="task=text_to_image"):
        validate_request({"prompt": "x", "harmony_requires": {"task": "image_to_image"}})


@pytest.mark.parametrize("remove", [
    "control_image", "controlnet_conditioning_scale", "depth_guide",
])
def test_image_scale_and_requirement_are_all_required(remove):
    body = _guided_request()
    if remove == "depth_guide":
        del body["harmony_requires"][remove]
    else:
        del body[remove]
    with pytest.raises(ValueError, match="paired"):
        validate_request(body)


@pytest.mark.parametrize("present", ["control_image", "controlnet_conditioning_scale", "depth_guide"])
def test_no_single_depth_field_can_activate_guidance(present):
    body = {"prompt": "x"}
    if present == "depth_guide":
        body["harmony_requires"] = {"depth_guide": True}
    elif present == "control_image":
        body[present] = _control_image()
    else:
        body[present] = 0.8
    with pytest.raises(ValueError, match="paired"):
        validate_request(body)


@pytest.mark.parametrize("key", [
    "model", "revision", "controlnet_model", "controlnet_revision",
    "controlnet_config_sha256", "quantization",
])
def test_guided_requirements_name_every_exact_identity(key):
    body = _guided_request()
    del body["harmony_requires"][key]
    with pytest.raises(ValueError, match="exact identity"):
        validate_request(body)


@pytest.mark.parametrize("key,value", [
    ("revision", "short"),
    ("controlnet_revision", "A" * 40),
    ("controlnet_config_sha256", "A" * 64),
    ("model", ""),
    ("controlnet_model", None),
    ("quantization", 4),
])
def test_guided_identity_fields_are_well_formed(key, value):
    body = _guided_request()
    body["harmony_requires"][key] = value
    with pytest.raises(ValueError):
        validate_request(body)


@pytest.mark.parametrize("encoded", ["not base64!", " Zg==", "Zg", "Zh=="])
def test_control_image_requires_strict_base64(encoded):
    body = _guided_request()
    body["control_image"]["b64_json"] = encoded
    with pytest.raises(ValueError):
        validate_request(body)


def test_control_image_decoded_size_is_bounded():
    data = b"x" * (MAX_CONTROL_IMAGE_BYTES + 1)
    body = _guided_request()
    body["control_image"] = _control_image(data)
    with pytest.raises(ValueError, match="8 MiB"):
        validate_request(body)


@pytest.mark.parametrize("digest", ["A" * 64, "0" * 63, "g" * 64, "0" * 64])
def test_control_image_requires_exact_lowercase_sha256(digest):
    body = _guided_request()
    body["control_image"]["sha256"] = digest
    with pytest.raises(ValueError):
        validate_request(body)


def test_control_image_must_be_png_with_declared_media_type():
    stream = io.BytesIO()
    Image.new("RGB", (768, 432)).save(stream, format="JPEG")
    body = _guided_request()
    body["control_image"] = _control_image(stream.getvalue())
    with pytest.raises(ValueError, match="PNG"):
        validate_request(body)
    body = _guided_request()
    body["control_image"]["media_type"] = "image/jpeg"
    with pytest.raises(ValueError, match="media_type"):
        validate_request(body)


def test_control_image_must_decode_as_one_rgb_readable_image():
    body = _guided_request()
    body["control_image"] = _control_image(_png_bytes()[:100])
    with pytest.raises(ValueError):
        validate_request(body)

    animated = _png_bytes(save_all=True, append_images=[Image.new("RGB", (768, 432))])
    body = _guided_request()
    body["control_image"] = _control_image(animated)
    with pytest.raises(ValueError, match="exactly one"):
        validate_request(body)


def test_control_image_dimensions_must_equal_requested_output_dimensions():
    body = _guided_request()
    body["width"] = 512
    with pytest.raises(ValueError, match="dimensions"):
        validate_request(body)


def test_guided_requirements_match_the_complete_advertised_identity():
    requirements = validate_request(_guided_request())["harmony_requires"]
    attributes = dict(requirements)
    assert satisfies(attributes, requirements)
    for key in (
        "revision", "depth_guide", "controlnet_model", "controlnet_revision",
        "controlnet_config_sha256", "quantization",
    ):
        changed = dict(attributes)
        changed[key] = "changed"
        assert not satisfies(changed, requirements), key


def test_legacy_attributes_do_not_satisfy_a_new_base_revision_requirement():
    legacy = {"class": "imagegen", "task": "text_to_image", "model": "Qwen/Qwen-Image-2512"}
    assert satisfies(legacy, {"class": "imagegen", "task": "text_to_image"})
    assert not satisfies(legacy, {"revision": BASE_REVISION})


def test_a_refusal_carries_the_planners_own_reason():
    refused = {"granted": False, "reason": "the planner could not place it on any device", "defer_reason": "qwen_image_2512 needs 20.9 GB, gpu1 has 20.1 GB free"}
    with pytest.raises(RuntimeError, match="could not place it on any device: qwen_image_2512 needs 20.9 GB"):
        resolve_worker({}, refused, {})
