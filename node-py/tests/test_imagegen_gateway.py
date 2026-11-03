import base64
import copy
import hashlib
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from livestack_node.imagegen.gateway import create_app


def _guided_body():
    stream = io.BytesIO()
    Image.new("RGB", (768, 432), (32, 64, 96)).save(stream, format="PNG")
    data = stream.getvalue()
    return {
        "prompt": "A grounded landscape",
        "width": 768,
        "height": 432,
        "control_image": {
            "media_type": "image/png",
            "b64_json": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "controlnet_conditioning_scale": 0.8,
        "harmony_requires": {
            "class": "imagegen",
            "task": "text_to_image",
            "depth_guide": True,
            "model": "Qwen/Qwen-Image-2512",
            "revision": "4" * 40,
            "controlnet_model": "test/Qwen-ControlNet",
            "controlnet_revision": "5" * 40,
            "controlnet_config_sha256": "6" * 64,
            "quantization": "nf4",
        },
    }


def _generation_settings():
    return {
        "steps": 50, "true_cfg_scale": 4.0, "negative_prompt": "",
        "text_encoder": "CPU FP32", "max_sequence_length": 512,
    }


def _guided_result(body):
    requirements = body["harmony_requires"]
    return {
        "data": [{"b64_json": "png"}],
        "harmony": {
            "model": requirements["model"],
            "revision": requirements["revision"],
            "unit": "qwen_image_2512_depth_nf4",
            "device_id": "tower/gpu1",
            "width": body["width"],
            "height": body["height"],
            "depth_guide": {
                "image_sha256": body["control_image"]["sha256"],
                "conditioning_scale": body["controlnet_conditioning_scale"],
                "controlnet_model": requirements["controlnet_model"],
                "controlnet_revision": requirements["controlnet_revision"],
                "controlnet_config_sha256": requirements["controlnet_config_sha256"],
                "controlnet_quantization": "bf16",
                "control_guidance_start": 0.0,
                "control_guidance_end": 1.0,
            },
            "generation_settings": _generation_settings(),
        },
    }


def _guided_call(body, calls, result=None, error=None, controlnet_quantization="bf16"):
    attributes = dict(body["harmony_requires"])
    attributes.update(_generation_settings())
    attributes["controlnet_quantization"] = controlnet_quantization

    def call(base, method, path, request=None, token=None, timeout=None):
        calls.append((base, method, path, request, token))
        if path == "/admit":
            assert request["requires"] == body["harmony_requires"]
            return {"granted": True, "device_id": "tower/gpu1",
                    "kind": "qwen_image_2512_depth_nf4"}
        if path == "/fleet":
            return {"hosts": {"tower": {"nodes": [{
                "state": "fresh", "device_id": "tower/gpu1",
                "peer": "http://tower:8210/livestack",
                "units": [{"kind": "qwen_image_2512_depth_nf4",
                           "attributes": attributes}],
            }]}}}
        assert path == "/v1/images/generations"
        if error is not None:
            raise error
        return copy.deepcopy(_guided_result(body) if result is None else result)

    return call


@pytest.fixture
def setup(tmp_path):
    worker = tmp_path / "worker.token"
    fleet = tmp_path / "fleet.token"
    worker.write_text("worker-secret")
    fleet.write_text("fleet-secret")
    config = {"worker_token_file": str(worker), "fleet_token_file": str(fleet), "fleet": "http://fleet"}
    calls = []

    def call(base, method, path, body=None, token=None, timeout=None):
        calls.append((base, method, path, body, token, timeout))
        if path == "/admit":
            assert token == "fleet-secret"
            assert body["owner"] == "acct_harmony-image"
            assert body["requires"]["params_b>="] == 20
            return {"granted": True, "device_id": "tower/gpu1", "kind": "qwen"}
        if path == "/fleet":
            return {"hosts": {"tower": {"nodes": [{"state": "fresh", "device_id": "tower/gpu1", "peer": "http://tower:8210/livestack", "units": [{"kind": "qwen", "attributes": {"class": "imagegen", "task": "text_to_image", "params_b": 20}}]}]}}}
        assert base == "http://tower:8210"
        assert token == "worker-secret"
        return {"data": [{"b64_json": "png"}], "harmony": {"unit": "qwen", "device_id": "tower/gpu1"}}
    return config, calls, call


def test_gateway_passes_same_prompt_and_records_real_grant(setup):
    config, calls, call = setup
    client = TestClient(create_app(config, call=call))
    body = {"prompt": "An astronaut tending a garden", "harmony_requires": {"params_b>=": 20}, "seed": 42}
    response = client.post("/v1/images/generations", json=body, headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200
    assert calls[-1][3]["prompt"] == body["prompt"]
    assert calls[-1][3]["seed"] == 42
    assert response.json()["harmony"]["fleet_grant"]["kind"] == "qwen"


def test_gateway_forwards_configured_generation_timeout(setup):
    config, calls, call = setup
    config["request_timeout_s"] = 7200
    body = {"prompt": "An astronaut tending a garden", "harmony_requires": {"params_b>=": 20}}
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body, headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200
    assert calls[-1][-1] == 7200


def test_gateway_auth_precedes_broker_call(setup):
    config, calls, call = setup
    response = TestClient(create_app(config, call=call)).post("/v1/images/generations", json={"prompt": "x"})
    assert response.status_code == 401
    assert calls == []


def test_worker_cannot_return_a_different_model(setup):
    config, _, call = setup
    def substitution(*args, **kwargs):
        result = call(*args, **kwargs)
        if args[2] == "/v1/images/generations":
            result["harmony"]["unit"] = "z-image"
        return result
    response = TestClient(create_app(config, call=substitution)).post(
        "/v1/images/generations", json={"prompt": "x", "harmony_requires": {"params_b>=": 20}},
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 503


def test_gateway_verifies_int8_controlnet_against_selected_unit_not_request(setup):
    config, _, _ = setup
    body = _guided_body()
    result = _guided_result(body)
    result["harmony"]["depth_guide"]["controlnet_quantization"] = "int8"
    calls = []
    call = _guided_call(body, calls, result=result, controlnet_quantization="int8")
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200, response.text
    assert "controlnet_quantization" not in body["harmony_requires"]
    assert response.json()["harmony"]["depth_guide"]["controlnet_quantization"] == "int8"


def test_gateway_serializes_the_validated_guide_as_json(setup):
    config, _, _ = setup
    body = _guided_body()
    calls = []
    call = _guided_call(body, calls)
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200, response.text
    assert [call_row[2] for call_row in calls] == [
        "/admit", "/fleet", "/v1/images/generations",
    ]
    forwarded = calls[-1][3]
    assert forwarded["control_image"] == body["control_image"]
    assert forwarded["controlnet_conditioning_scale"] == 0.8
    json.dumps(forwarded, allow_nan=False)
    harmony = response.json()["harmony"]
    assert harmony["generation_settings"] == _generation_settings()
    assert set(harmony["fleet_grant"]) == {"granted", "device_id", "kind"}


@pytest.mark.parametrize("path,value", [
    (("controlnet_conditioning_scale",), True),
    (("harmony_requires", "depth_guide"), 1),
    (("harmony_requires", "task"), "image_to_image"),
    (("harmony_requires", "class"), "llm"),
    (("control_image", "sha256"), "0" * 64),
    (("control_image", "b64_json"), "not base64!"),
])
def test_invalid_guided_json_is_refused_before_admission(setup, path, value):
    config, calls, call = setup
    body = _guided_body()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 400
    assert calls == []


def test_missing_guided_scale_is_refused_before_admission(setup):
    config, calls, call = setup
    body = _guided_body()
    del body["controlnet_conditioning_scale"]
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 400
    assert calls == []


_GUIDED_RESULT_PATHS = [
    ("model",), ("revision",), ("unit",), ("device_id",),
    ("width",), ("height",), ("depth_guide",), ("generation_settings",),
    ("depth_guide", "image_sha256"),
    ("depth_guide", "conditioning_scale"),
    ("depth_guide", "controlnet_model"),
    ("depth_guide", "controlnet_revision"),
    ("depth_guide", "controlnet_config_sha256"),
    ("depth_guide", "controlnet_quantization"),
    ("depth_guide", "control_guidance_start"),
    ("depth_guide", "control_guidance_end"),
    ("generation_settings", "steps"),
    ("generation_settings", "true_cfg_scale"),
    ("generation_settings", "negative_prompt"),
    ("generation_settings", "text_encoder"),
    ("generation_settings", "max_sequence_length"),
]


@pytest.mark.parametrize("path", _GUIDED_RESULT_PATHS)
@pytest.mark.parametrize("missing", [False, True], ids=["wrong", "missing"])
def test_gateway_rejects_every_guided_evidence_and_setting_mismatch(
        setup, path, missing):
    config, _, _ = setup
    body = _guided_body()
    result = _guided_result(body)
    target = result["harmony"]
    for key in path[:-1]:
        target = target[key]
    if missing:
        del target[path[-1]]
    else:
        target[path[-1]] = "mismatch"
    calls = []
    call = _guided_call(body, calls, result=result)
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 503
    assert [call_row[2] for call_row in calls] == [
        "/admit", "/fleet", "/v1/images/generations",
    ]


def test_guided_worker_failure_is_not_retried_or_substituted(setup):
    config, _, _ = setup
    body = _guided_body()
    calls = []
    call = _guided_call(body, calls, error=RuntimeError("guided inference failed"))
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 503
    assert "guided inference failed" in response.json()["detail"]
    assert [call_row[2] for call_row in calls] == [
        "/admit", "/fleet", "/v1/images/generations",
    ]


@pytest.mark.parametrize("device_ids", [["tower/gpu1"], ["tower/gpu1", "tower/gpu0"]])
def test_exclusive_unit_leases_cover_the_worker_call(setup, monkeypatch, device_ids):
    config, _, _ = setup
    config["exclusive_units"] = {"qwen_image_2512_depth_nf4": {
        "broker": "http://host-broker", "owner": "harmony-image", "device_ids": device_ids,
        "min_free_bytes": {device: 4_000_000_000 + index * 17_000_000_000
                           for index, device in enumerate(device_ids)},
        "wait_timeout_s": 240,
        "ttl_seconds": 120,
    }}
    profile = config["exclusive_units"]["qwen_image_2512_depth_nf4"]
    body = {**_guided_body(), "harmony_wait_s": 1800}
    original_call = _guided_call(body, [])
    events = []
    forwarded = []

    class Lease:
        def __init__(self, device_id):
            self.device_id = device_id
            self.lease_id = "device-lease-" + device_id.rsplit("/", 1)[-1]

        def __enter__(self):
            events.append("enter:" + self.device_id)
            return {"granted": True, "lease_id": self.lease_id,
                    "device_id": self.device_id, "state": "exclusive"}

        def __exit__(self, *args):
            events.append("exit:" + self.device_id)

    def acquire(*args, **kwargs):
        events.append(("acquire", kwargs))
        return Lease(kwargs["device_id"])

    def call(base, method, path, request=None, token=None, timeout=None, lease_id=None):
        events.append(path)
        if path == "/admit":
            # Observe-only fleet admission picks the unit before any lease exists.
            assert "device_lease_ids" not in request
        result = original_call(base, method, path, request, token=token)
        if path == "/v1/images/generations":
            forwarded.append(lease_id)
            result["harmony"]["device_leases"] = [
                {"lease_id": "device-lease-" + device.rsplit("/", 1)[-1], "device_id": device}
                for device in device_ids]
        return result

    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", acquire)
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200, response.text
    acquisitions = [event[1] for event in events if isinstance(event, tuple)]
    assert [event["device_id"] for event in acquisitions] == device_ids
    assert [event["min_free_bytes"] for event in acquisitions] == [
        profile["min_free_bytes"][device] for device in device_ids]
    assert all(event["brokers"] == ["http://host-broker"] and event["owner_id"] == "harmony-image"
               for event in acquisitions)
    # The caller's harmony_wait_s carries over: a busy device queues, not refuses.
    assert all(0 < event["queue_wait_s"] <= body["harmony_wait_s"] for event in acquisitions)
    order = [event if isinstance(event, str) else "acquire" for event in events]
    assert order.index("/fleet") < order.index("acquire")
    assert order.index("/v1/images/generations") < order.index("exit:" + device_ids[0])
    assert forwarded == [json.dumps([
        "device-lease-" + device.rsplit("/", 1)[-1] for device in device_ids], separators=(",", ":"))]
    harmony = response.json()["harmony"]
    assert [grant["device_id"] for grant in harmony["device_grants"]] == device_ids


def test_units_without_a_lease_profile_never_take_device_leases(setup, monkeypatch):
    config, calls, call = setup
    config["exclusive_units"] = {"sdxl_depth_int8": {
        "broker": "http://host-broker", "owner": "harmony-image",
        "device_ids": ["tower/gpu1"], "min_free_bytes": 1}}

    def refuse(*args, **kwargs):
        raise AssertionError("a lease-free unit must not lease a device")

    monkeypatch.setattr("livestack_node.imagegen.gateway.exclusive_lease", refuse)
    response = TestClient(create_app(config, call=call)).post(
        "/v1/images/generations", json={"prompt": "x", "harmony_requires": {"params_b>=": 20}},
        headers={"Authorization": "Bearer worker-secret"})
    assert response.status_code == 200, response.text
    assert "device_grants" not in response.json()["harmony"]


@pytest.mark.parametrize("profile", [
    {"owner": "harmony-image", "device_ids": ["a"], "min_free_bytes": 1},
    {"broker": "http://b", "owner": "harmony-image", "device_ids": [], "min_free_bytes": 1},
    {"broker": "http://b", "owner": "harmony-image", "device_ids": ["a"], "min_free_bytes": {"b": 1}},
    {"broker": "http://b", "owner": "harmony-image", "device_ids": ["a"], "min_free_bytes": 1, "ttl_seconds": 0},
])
def test_malformed_lease_profiles_are_refused_at_startup(setup, profile):
    config, _, call = setup
    config["exclusive_units"] = {"unit": profile}
    with pytest.raises(ValueError):
        create_app(config, call=call)


def test_a_request_that_can_wait_says_so_to_harmony_and_is_asked_once(setup):
    config, calls, call = setup
    client = TestClient(create_app(config, call=call))
    body = {"prompt": "x", "harmony_requires": {"params_b>=": 20}, "harmony_wait_s": 60}
    assert client.post("/v1/images/generations", json=body, headers={"Authorization": "Bearer worker-secret"}).status_code == 200
    admits = [c for c in calls if c[2] == "/admit"]
    assert len(admits) == 1 and admits[0][3]["wait_s"] == 60


def test_a_request_that_runs_out_of_patience_gets_the_planners_reason(setup):
    config, _, real = setup
    def call(base, method, path, body=None, token=None, timeout=None):
        if path == "/admit":
            return {"granted": False, "reason": "the planner could not place it on any device", "defer_reason": "no unit satisfies"}
        return real(base, method, path, body, token)
    client = TestClient(create_app(config, call=call))
    body = {"prompt": "x", "harmony_requires": {"params_b>=": 20}, "harmony_wait_s": 12}
    r = client.post("/v1/images/generations", json=body, headers={"Authorization": "Bearer worker-secret"})
    assert r.status_code == 503 and "no unit satisfies" in r.json()["detail"]
    assert client.post("/v1/images/generations", json={**body, "harmony_wait_s": -1}, headers={"Authorization": "Bearer worker-secret"}).status_code == 400
