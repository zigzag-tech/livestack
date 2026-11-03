import base64
import hashlib
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from livestack_node.imagegen import worker


BASE_REVISION = "a" * 40
CONTROLNET_REVISION = "b" * 40


def _legacy_config(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret")
    return {
        "token_file": str(token), "params_b": 6,
        "model": "Tongyi-MAI/Z-Image-Turbo", "unit": "z",
        "resident_bytes": 1, "host_id": "joe", "port": 8210,
        "device_id": "joe/gpu", "broker": "http://broker",
    }


def _depth_config(tmp_path):
    config = _legacy_config(tmp_path)
    base_snapshot = tmp_path / "base" / BASE_REVISION
    control_snapshot = tmp_path / "control" / CONTROLNET_REVISION
    base_snapshot.mkdir(parents=True)
    control_snapshot.mkdir(parents=True)
    raw_config = json.dumps({"_class_name": "QwenImageControlNetModel"}).encode()
    weight = b"temporary-controlnet-weights"
    (control_snapshot / "config.json").write_bytes(raw_config)
    (control_snapshot / "diffusion_pytorch_model.safetensors").write_bytes(weight)
    config.update(
        params_b=20, model="Qwen/Qwen-Image-2512",
        unit="qwen_image_2512_depth_nf4", revision=BASE_REVISION,
        snapshot=str(base_snapshot), resident_bytes=20_000,
        quantization="nf4", steps=50,
        depth_guide={
            "model": "test/Qwen-ControlNet",
            "revision": CONTROLNET_REVISION,
            "snapshot": str(control_snapshot),
            "config_sha256": hashlib.sha256(raw_config).hexdigest(),
            "weight_sha256": hashlib.sha256(weight).hexdigest(),
            "weight_bytes": len(weight),
        },
    )
    return config


def _capture_attach(monkeypatch):
    captured = {}

    def fake_attach(app, **kwargs):
        captured.update(kwargs)
        return None, None

    monkeypatch.setattr(worker, "attach", fake_attach)
    return captured


def _write_control_config(config, raw, update_hash):
    depth = config["depth_guide"]
    Path(depth["snapshot"], "config.json").write_bytes(raw)
    if update_hash:
        depth["config_sha256"] = hashlib.sha256(raw).hexdigest()


def _guided_body(config, prompt="A grounded landscape", scale=0.8, seed=1863):
    stream = io.BytesIO()
    Image.new("RGB", (256, 256), (32, 64, 96)).save(stream, format="PNG")
    data = stream.getvalue()
    depth = config["depth_guide"]
    return {
        "prompt": prompt, "width": 256, "height": 256, "seed": seed,
        "control_image": {
            "media_type": "image/png",
            "b64_json": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "controlnet_conditioning_scale": scale,
        "harmony_requires": {
            "class": "imagegen", "task": "text_to_image", "depth_guide": True,
            "model": config["model"], "revision": config["revision"],
            "controlnet_model": depth["model"],
            "controlnet_revision": depth["revision"],
            "controlnet_config_sha256": depth["config_sha256"],
            "quantization": config["quantization"],
        },
    }


def _generation_settings(config):
    return {
        "steps": config["steps"],
        "true_cfg_scale": config.get("true_cfg_scale", 4.0),
        "negative_prompt": config.get("negative_prompt", ""),
        "text_encoder": "CPU FP32",
        "max_sequence_length": config.get("max_sequence_length", 512),
    }


def _runtime_metrics(config, request):
    evidence = None
    if "control_image" in request:
        depth = config["depth_guide"]
        evidence = {
            "image_sha256": request["control_image"]["sha256"],
            "conditioning_scale": request["controlnet_conditioning_scale"],
            "controlnet_model": depth["model"],
            "controlnet_revision": depth["revision"],
            "controlnet_config_sha256": depth["config_sha256"],
            "controlnet_quantization": depth.get("quantization", "bf16"),
            "control_guidance_start": 0.0,
            "control_guidance_end": 1.0,
        }
    return {
        "depth_guide": evidence,
        "generation_settings": _generation_settings(config),
    }


def _nonblank(size):
    return Image.linear_gradient("L").resize(size).convert("RGB")


class _Coordinator:
    def __init__(self):
        self.acquired = []
        self.released = []
        self.active = set()
        self.lock = threading.Lock()

    def acquire_lease(self, *args, **kwargs):
        with self.lock:
            lease_id = f"lease-{len(self.acquired) + 1}"
            self.acquired.append(lease_id)
            self.active.add(lease_id)
            return {"lease_id": lease_id}

    def release_lease(self, lease_id):
        with self.lock:
            assert lease_id in self.active
            self.active.remove(lease_id)
            self.released.append(lease_id)


class _Manager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def run(self, unit_name, thunk):
        self.calls.append(unit_name)
        return thunk(self.runtime)


class _GuidedRuntime:
    def __init__(self, config, fail=False, output_size=None):
        self.config = config
        self.fail = fail
        self.output_size = output_size
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def generate(self, request):
        with self.lock:
            self.calls.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            if self.fail:
                raise RuntimeError("guided inference failed")
            size = self.output_size or (request["width"], request["height"])
            return _nonblank(size), _runtime_metrics(self.config, request)
        finally:
            with self.lock:
                self.active -= 1


def _attach_runtime(monkeypatch, runtime):
    manager = _Manager(runtime)
    coordinator = _Coordinator()
    monkeypatch.setattr(worker, "attach", lambda *a, **k: (manager, coordinator))
    return manager, coordinator


def test_request_holds_lease_during_admission_and_releases_on_refusal(tmp_path, monkeypatch):
    active = []

    class Coordinator:
        def acquire_lease(self, *args, **kwargs):
            active.append("lease")
            return {"lease_id": "lease"}

        def release_lease(self, lease_id):
            active.remove(lease_id)

    monkeypatch.setattr(worker, "attach", lambda *args, **kwargs: (None, Coordinator()))

    def refused(*args, **kwargs):
        assert active == ["lease"], "protect the image unit before broker warm can return"
        return {"granted": False, "reason": "capacity busy"}

    monkeypatch.setattr(worker, "admit", refused)
    client = TestClient(worker.create_app(_legacy_config(tmp_path)))
    response = client.post("/v1/images/generations", json={"prompt": "apple"}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    assert active == []


def test_worker_rejects_resolution_above_qualified_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "attach", lambda *a, **k: (None, None))
    config = _legacy_config(tmp_path)
    config["max_dimension"] = 768
    response = TestClient(worker.create_app(config)).post("/v1/images/generations", json={"prompt": "apple", "width": 1024, "height": 1024}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 422
    assert "768" in response.json()["detail"]


def test_legacy_worker_without_revision_keeps_legacy_attributes(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _legacy_config(tmp_path)
    worker.create_app(config)
    unit = captured["units"]["z"]
    assert "revision" not in unit.attributes
    assert "depth_guide" not in unit.attributes
    assert "controlnet_model" not in unit.attributes
    assert unit.footprint == config["resident_bytes"]


def test_configured_base_revision_is_advertised_for_unguided_worker(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _legacy_config(tmp_path)
    config["revision"] = BASE_REVISION
    worker.create_app(config)
    unit = captured["units"]["z"]
    assert unit.attributes["revision"] == BASE_REVISION
    assert "depth_guide" not in unit.attributes
    assert unit.footprint == config["resident_bytes"]


@pytest.mark.parametrize("unit_name,quantization", [
    ("qwen_image_2512_depth_nf4", "nf4"),
    ("qwen_image_2512_depth_int8", "int8"),
])
def test_verified_depth_worker_advertises_identity_and_combined_footprint(
        tmp_path, monkeypatch, capsys, unit_name, quantization):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config.update(unit=unit_name, quantization=quantization)
    runtime_calls = []
    worker.create_app(config, runtime_factory=lambda *a, **k: runtime_calls.append((a, k)))
    unit = captured["units"][config["unit"]]
    depth = config["depth_guide"]
    assert unit.attributes["revision"] == config["revision"]
    assert unit.attributes["depth_guide"] is True
    assert unit.attributes["controlnet_model"] == depth["model"]
    assert unit.attributes["controlnet_revision"] == depth["revision"]
    assert unit.attributes["controlnet_config_sha256"] == depth["config_sha256"]
    assert unit.attributes["controlnet_quantization"] == "bf16"
    assert unit.footprint == config["resident_bytes"] + depth["weight_bytes"]
    for key, want in _generation_settings(config).items():
        assert unit.attributes[key] == want
    assert "generation_settings" not in unit.attributes
    assert runtime_calls == []

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and len(lines[0]) < 2048
    record = json.loads(lines[0])
    assert record["event"] == "image_depth_startup"
    assert record["status"] == "verified"
    assert record["revision"] == config["revision"]
    assert record["controlnet_revision"] == depth["revision"]
    assert record["unit"] == config["unit"]
    assert record["quantization"] == config["quantization"]
    assert record["config_sha256"] == depth["config_sha256"]
    assert record["weight_sha256"] == depth["weight_sha256"]
    assert record["weight_bytes"] == depth["weight_bytes"]


def test_flux2_integrated_controller_is_not_charged_twice(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config.update(model="black-forest-labs/FLUX.2-dev", unit="flux_2_dev_depth_int8")
    monkeypatch.setattr(worker, "_verify_depth_startup",
                        lambda _config: config["depth_guide"]["weight_bytes"])
    worker.create_app(config)
    assert captured["units"][config["unit"]].footprint == config["resident_bytes"]


def test_a_flux2_worker_offers_inpainting_and_a_qwen_worker_does_not(tmp_path, monkeypatch):
    """The FLUX.2 runtime inpaints with the controller it already loads, so its unit offers `task: inpaint` and the
    specialist `inpaint` (an inpaint request is placed only there); other image units offer text-to-image only."""
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config.update(model="black-forest-labs/FLUX.2-dev", unit="flux_2_dev_depth_int8")
    monkeypatch.setattr(worker, "_verify_depth_startup", lambda _config: config["depth_guide"]["weight_bytes"])
    worker.create_app(config)
    unit = captured["units"][config["unit"]]
    assert unit.attributes["task"] == ["text_to_image", "inpaint"] and unit.attributes["inpaint"] is True
    assert unit.attributes["depth_guide"] is True
    captured = _capture_attach(monkeypatch)
    (tmp_path / "qwen").mkdir()
    qwen = _depth_config(tmp_path / "qwen")
    worker.create_app(qwen, runtime_factory=lambda *a, **k: None)
    assert captured["units"][qwen["unit"]].attributes["task"] == "text_to_image"
    assert "inpaint" not in captured["units"][qwen["unit"]].attributes


def test_int8_controlnet_uses_quantized_footprint_and_preserves_source_identity(
        tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config.update(unit="qwen_image_2512_depth_int8", quantization="int8")
    config["depth_guide"]["quantization"] = "int8"
    worker.create_app(config)
    unit = captured["units"][config["unit"]]
    assert unit.attributes["quantization"] == "int8"
    assert unit.attributes["controlnet_quantization"] == "int8"
    source_bytes = config["depth_guide"]["weight_bytes"]
    assert unit.footprint == config["resident_bytes"] + (source_bytes + 1) // 2


def test_worker_passes_depth_config_to_runtime_only_when_configured(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config.update(true_cfg_scale=4.5, negative_prompt="avoid artifacts",
                  max_sequence_length=384)
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    worker.create_app(config, runtime_factory=factory)
    captured["units"][config["unit"]].load()
    assert calls == [((config["snapshot"], config["model"]), {
        "steps": config["steps"], "threads": 8, "quantization": "nf4",
        "unit": config["unit"], "depth_guide": config["depth_guide"],
        "true_cfg_scale": 4.5, "negative_prompt": "avoid artifacts",
        "max_sequence_length": 384,
    })]


def test_legacy_runtime_constructor_receives_no_depth_arguments(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _legacy_config(tmp_path)
    config.update(snapshot=str(tmp_path / "legacy"), steps=4)
    calls = []

    def legacy_factory(snapshot, model, *, steps, threads=8, quantization="nf4"):
        calls.append((snapshot, model, steps, threads, quantization))
        return object()

    worker.create_app(config, runtime_factory=legacy_factory)
    captured["units"][config["unit"]].load()
    assert calls == [(config["snapshot"], config["model"], 4, 8, "nf4")]


@pytest.mark.parametrize("mutation", [
    "wrong_base", "short_base_revision", "base_snapshot_mismatch",
    "wrong_kind", "int4", "nf4_kind_int8", "int8_kind_nf4",
    "short_control_revision", "control_snapshot_mismatch", "bad_depth_type",
    "controlnet_nf4", "missing_config", "malformed_config", "changed_config", "wrong_config_class",
    "missing_weight", "weight_size_mismatch", "weight_hash_mismatch",
    "configured_weight_size_mismatch", "long_kind",
])
def test_invalid_depth_startup_never_attaches_or_constructs_runtime(
        tmp_path, monkeypatch, capsys, mutation):
    config = _depth_config(tmp_path)
    depth = config["depth_guide"]
    snapshot = Path(depth["snapshot"])
    if mutation == "wrong_base":
        config["model"] = "test/not-qwen"
    elif mutation == "short_base_revision":
        config["revision"] = "short"
    elif mutation == "base_snapshot_mismatch":
        config["snapshot"] = str(tmp_path / "base" / ("c" * 40))
    elif mutation == "wrong_kind":
        config["unit"] = "qwen_image_2512"
    elif mutation == "int4":
        config["quantization"] = "int4"
    elif mutation == "nf4_kind_int8":
        config["quantization"] = "int8"
    elif mutation == "int8_kind_nf4":
        config["unit"] = "qwen_image_2512_depth_int8"
    elif mutation == "short_control_revision":
        depth["revision"] = "short"
    elif mutation == "control_snapshot_mismatch":
        depth["snapshot"] = str(tmp_path / "control" / ("d" * 40))
    elif mutation == "bad_depth_type":
        config["depth_guide"] = True
    elif mutation == "controlnet_nf4":
        depth["quantization"] = "nf4"
    elif mutation == "missing_config":
        (snapshot / "config.json").unlink()
    elif mutation == "malformed_config":
        _write_control_config(config, b"{", update_hash=True)
    elif mutation == "changed_config":
        _write_control_config(
            config, b'{"_class_name":"QwenImageControlNetModel","changed":true}',
            update_hash=False)
    elif mutation == "wrong_config_class":
        _write_control_config(
            config, json.dumps({"_class_name": "OtherControlNet"}).encode(),
            update_hash=True)
    elif mutation == "missing_weight":
        (snapshot / "diffusion_pytorch_model.safetensors").unlink()
    elif mutation == "weight_size_mismatch":
        with (snapshot / "diffusion_pytorch_model.safetensors").open("ab") as handle:
            handle.write(b"x")
    elif mutation == "weight_hash_mismatch":
        weight_path = snapshot / "diffusion_pytorch_model.safetensors"
        weight = weight_path.read_bytes()
        weight_path.write_bytes(bytes([weight[0] ^ 1]) + weight[1:])
    elif mutation == "configured_weight_size_mismatch":
        depth["weight_bytes"] += 1
    elif mutation == "long_kind":
        config["unit"] = "x" * 10000

    attach_calls = []
    runtime_calls = []
    monkeypatch.setattr(worker, "attach",
                        lambda *a, **k: attach_calls.append((a, k)))
    with pytest.raises(worker.DepthConfigurationError):
        worker.create_app(
            config, runtime_factory=lambda *a, **k: runtime_calls.append((a, k)))
    assert attach_calls == []
    assert runtime_calls == []

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and len(lines[0]) < 2048
    record = json.loads(lines[0])
    assert record["event"] == "image_depth_startup"
    assert record["status"] == "refused"
    assert record["reason"]
    assert record["revision"] == str(config.get("revision"))[:128]
    assert record["unit"] == str(config.get("unit"))[:128]
    assert record["quantization"] == config.get("quantization", "nf4")


def test_unconfigured_worker_omits_all_generation_setting_attributes(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    worker.create_app(_legacy_config(tmp_path))
    attributes = captured["units"]["z"].attributes
    assert not {"steps", "true_cfg_scale", "negative_prompt", "text_encoder",
                "max_sequence_length", "generation_settings"} & set(attributes)


def test_qwen_text_worker_advertises_generation_settings_without_depth(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _legacy_config(tmp_path)
    config.update(model=worker.QWEN_IMAGE_MODEL, steps=50, negative_prompt="quality negative")
    worker.create_app(config)
    attributes = captured["units"]["z"].attributes
    assert attributes["negative_prompt"] == "quality negative"
    assert attributes["steps"] == config["steps"]
    assert attributes["true_cfg_scale"] == 4.0
    assert "depth_guide" not in attributes
    assert "controlnet_model" not in attributes


def test_worker_returns_actual_dimensions_and_runtime_evidence(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    config.update(true_cfg_scale=4.5, negative_prompt="avoid artifacts",
                  max_sequence_length=384)
    runtime = _GuidedRuntime(config, output_size=(512, 256))
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    monkeypatch.setattr(worker, "admit", lambda *a, **k: {
        "granted": True, "device_id": config["device_id"], "kind": config["unit"],
    })
    body = _guided_body(config)
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations", json=body,
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    harmony = response.json()["harmony"]
    assert (harmony["width"], harmony["height"]) == (512, 256)
    assert harmony["depth_guide"] == _runtime_metrics(config, body)["depth_guide"]
    assert harmony["generation_settings"] == _generation_settings(config)
    decoded = base64.b64decode(response.json()["data"][0]["b64_json"])
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.size == (512, 256)
    assert manager.calls == [config["unit"]]
    assert coordinator.acquired == coordinator.released


def test_worker_unguided_result_reports_null_depth_on_depth_runtime(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    runtime = _GuidedRuntime(config)
    _attach_runtime(monkeypatch, runtime)
    monkeypatch.setattr(worker, "admit", lambda *a, **k: {
        "granted": True, "device_id": config["device_id"], "kind": config["unit"],
    })
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations",
        json={"prompt": "ordinary text", "width": 256, "height": 256},
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    harmony = response.json()["harmony"]
    assert harmony["depth_guide"] is None
    assert harmony["generation_settings"] == _generation_settings(config)


def _recolor_guide(body, color):
    stream = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(stream, format="PNG")
    data = stream.getvalue()
    body["control_image"] = {
        "media_type": "image/png",
        "b64_json": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return body


def test_two_concurrent_guided_requests_are_serial_with_independent_receipts(
        tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    runtime = _GuidedRuntime(config)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    admissions = []

    def grant(*args, **kwargs):
        admissions.append((args, kwargs))
        return {"granted": True, "device_id": config["device_id"],
                "kind": config["unit"]}

    monkeypatch.setattr(worker, "admit", grant)
    client = TestClient(worker.create_app(config), raise_server_exceptions=False)
    bodies = [
        _guided_body(config, prompt="first guided request", scale=0.8, seed=1),
        _recolor_guide(
            _guided_body(config, prompt="second guided request", scale=0.9, seed=2),
            (120, 80, 40)),
    ]

    def post(body):
        return client.post("/v1/images/generations", json=body,
                           headers={"Authorization": "Bearer secret"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, bodies))
    assert [response.status_code for response in responses] == [200, 200]
    assert runtime.max_active == 1
    assert len(runtime.calls) == 2
    assert manager.calls == [config["unit"], config["unit"]]
    assert len(admissions) == 2
    assert len(coordinator.acquired) == 2
    assert sorted(coordinator.acquired) == sorted(coordinator.released)
    assert coordinator.active == set()

    for body, response in zip(bodies, responses):
        harmony = response.json()["harmony"]
        assert harmony["prompt"] == body["prompt"]
        assert harmony["seed"] == body["seed"]
        assert harmony["requirements"] == body["harmony_requires"]
        assert harmony["depth_guide"]["image_sha256"] == body["control_image"]["sha256"]
        assert (harmony["depth_guide"]["conditioning_scale"]
                == body["controlnet_conditioning_scale"])
        assert harmony["generation_settings"] == _generation_settings(config)


def test_guided_inference_failure_is_not_retried_or_substituted(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    runtime = _GuidedRuntime(config, fail=True)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    admissions = []
    monkeypatch.setattr(worker, "admit", lambda *a, **k: (
        admissions.append((a, k)) or {
            "granted": True, "device_id": config["device_id"],
            "kind": config["unit"],
        }))
    client = TestClient(worker.create_app(config), raise_server_exceptions=False)
    response = client.post(
        "/v1/images/generations", json=_guided_body(config),
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert "b64_json" not in response.text
    assert len(runtime.calls) == 1
    assert manager.calls == [config["unit"]]
    assert manager.runtime is runtime
    assert len(admissions) == 1
    assert len(coordinator.acquired) == 1
    assert coordinator.acquired == coordinator.released
    assert coordinator.active == set()


def test_free_and_reload_clear_old_depth_references_and_construct_a_new_runtime(
        tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    made = []

    class ReloadRuntime:
        def __init__(self):
            self.pipe = object()
            self.depth_pipe = object()
            self.controlnet = object()
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            self.pipe = self.depth_pipe = self.controlnet = None

    def factory(*args, **kwargs):
        instance = ReloadRuntime()
        made.append(instance)
        return instance

    worker.create_app(config, runtime_factory=factory)
    unit = captured["units"][config["unit"]]
    first = unit.load()
    unit.unload()
    assert first.close_calls == 1
    assert first.pipe is None and first.depth_pipe is None and first.controlnet is None

    second = unit.load()
    assert second is not first
    assert len(made) == 2
    assert second.pipe is not None
    assert second.depth_pipe is not None
    assert second.controlnet is not None
    unit.unload()
    assert second.close_calls == 1
    assert second.pipe is None and second.depth_pipe is None and second.controlnet is None


def test_fleet_token_is_passed_to_admission(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    token_path = tmp_path / "fleet.token"
    token_path.write_text("fleet-secret")
    config["fleet_token_file"] = str(token_path)
    runtime = _GuidedRuntime(config)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    calls = []

    def grant(*args, **kwargs):
        calls.append((args, kwargs))
        return {"granted": True, "device_id": config["device_id"], "kind": config["unit"]}

    monkeypatch.setattr(worker, "admit", grant)
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations", json=_guided_body(config),
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    assert calls[0][1]["token"] == "fleet-secret"
    assert coordinator.acquired == coordinator.released


def test_absent_fleet_token_preserves_unauthenticated_admission(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    runtime = _GuidedRuntime(config)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    calls = []

    def grant(*args, **kwargs):
        calls.append((args, kwargs))
        return {"granted": True, "device_id": config["device_id"], "kind": config["unit"]}

    monkeypatch.setattr(worker, "admit", grant)
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations", json=_guided_body(config),
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    assert calls[0][1]["token"] is None
    assert coordinator.acquired == coordinator.released


def test_split_worker_charges_only_primary_footprint_to_primary_placement(tmp_path, monkeypatch):
    captured = _capture_attach(monkeypatch)
    config = _depth_config(tmp_path)
    config["device_ids"] = [config["device_id"], "joe/gpu1"]
    worker.create_app(config)
    assert captured["units"][config["unit"]].footprint == config["resident_bytes"]


def test_exclusive_device_forwards_gateway_leases_to_local_admission(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    config["device_ids"] = [config["device_id"], "joe/gpu1"]
    config["exclusive_device"] = {
        "min_free_bytes": 24_000_000_000,
        "wait_timeout_s": 240,
        "ttl_seconds": 120,
    }
    runtime = _GuidedRuntime(config)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    admissions = []
    monkeypatch.setattr(worker, "admit", lambda *args, **kwargs: (
        admissions.append(kwargs["device_lease_ids"]) or {
            "granted": True, "device_id": config["device_id"], "kind": config["unit"]}))
    lease_ids = ["device-lease-test-0", "device-lease-test-1"]
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations", json=_guided_body(config),
        headers={"Authorization": "Bearer secret", "X-Harmony-Device-Lease": json.dumps(lease_ids)})
    assert response.status_code == 200, response.text
    assert admissions == [lease_ids]
    assert response.json()["harmony"]["device_leases"] == [
        {"lease_id": lease_id, "device_id": device_id}
        for lease_id, device_id in zip(lease_ids, config["device_ids"])]
    assert coordinator.acquired == coordinator.released


def test_exclusive_device_rejects_missing_gateway_lease(tmp_path, monkeypatch):
    config = _depth_config(tmp_path)
    config["exclusive_device"] = {"min_free_bytes": 24_000_000_000}
    runtime = _GuidedRuntime(config)
    manager, coordinator = _attach_runtime(monkeypatch, runtime)
    response = TestClient(worker.create_app(config)).post(
        "/v1/images/generations", json=_guided_body(config),
        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    assert manager.calls == []


def test_exclusive_device_rejects_invalid_memory_threshold(tmp_path):
    config = _depth_config(tmp_path)
    config["exclusive_device"] = {"min_free_bytes": 0}
    with pytest.raises(ValueError, match="min_free_bytes"):
        worker.create_app(config)
