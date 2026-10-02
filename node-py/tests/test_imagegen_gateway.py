import pytest
from fastapi.testclient import TestClient
from livestack_node.imagegen.gateway import create_app


@pytest.fixture
def setup(tmp_path):
    worker = tmp_path / "worker.token"
    fleet = tmp_path / "fleet.token"
    worker.write_text("worker-secret")
    fleet.write_text("fleet-secret")
    config = {"worker_token_file": str(worker), "fleet_token_file": str(fleet), "fleet": "http://fleet"}
    calls = []

    def call(base, method, path, body=None, token=None):
        calls.append((base, method, path, body, token))
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
