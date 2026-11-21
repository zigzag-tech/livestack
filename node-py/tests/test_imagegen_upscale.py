import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from livestack_node.imagegen import worker
from livestack_node.imagegen.contract import (
    MAX_UPSCALE_IMAGE_BYTES, MAX_UPSCALE_SIDE, validate_request,
)
from livestack_node.planner import Request, Unit, WorldState, candidate_kinds


def _png(width=1440, height=650):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (30, 60, 90)).save(stream, format="PNG")
    return stream.getvalue()


def _image(data=None):
    data = _png() if data is None else data
    return {"media_type": "image/png", "b64_json": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest()}


def _upscale_body(**overrides):
    body = {"init_image": _image(), "scale": 2}
    body.update(overrides)
    return body


def test_an_init_image_and_a_scale_make_an_upscale_request():
    """Upscaling is derived from what is sent, like inpainting: the task and the specialist
    attribute are set by the contract, so the worker never takes a generation request."""
    out = validate_request(_upscale_body())
    assert out["harmony_requires"] == {"class": "imagegen", "task": "upscale", "upscale": True}
    assert (out["width"], out["height"]) == (2880, 1300)
    assert out["init_size"] == [1440, 650]
    assert out["scale"] == 2


def test_the_scale_defaults_to_four():
    out = validate_request({"init_image": _image()})
    assert out["scale"] == 4
    assert (out["width"], out["height"]) == (5760, 2600)


@pytest.mark.parametrize("scale", [0, 1, 5, True, False, 2.0, "4", None, 10 ** 6])
def test_the_scale_is_an_integer_factor(scale):
    with pytest.raises(ValueError, match="scale"):
        validate_request(_upscale_body(scale=scale))


@pytest.mark.parametrize("drop", ["init_image", "mask_image"])
def test_an_image_without_its_partner_is_still_a_pairing_error(drop):
    body = {"init_image": _image(), "mask_image": _image(), "prompt": "x", "width": 1440, "height": 650}
    body.pop(drop)
    with pytest.raises(ValueError, match="init_image and mask_image must be paired"):
        validate_request(body)


@pytest.mark.parametrize("field,value", [
    ("prompt", "sharpen this"), ("control_image", _image()),
])
def test_an_upscale_request_takes_no_generation_fields(field, value):
    with pytest.raises(ValueError, match="takes no"):
        validate_request(_upscale_body(**{field: value}))


def test_a_mask_makes_it_an_inpaint_again():
    body = {"prompt": "continue the sign", "width": 640, "height": 384,
            "init_image": _image(_png(640, 384)), "mask_image": _image(_png(640, 384))}
    assert validate_request(body)["harmony_requires"]["task"] == "inpaint"


def test_stating_upscale_without_an_image_is_refused():
    with pytest.raises(ValueError, match="task=text_to_image"):
        validate_request({"prompt": "x", "harmony_requires": {"task": "upscale"}})


def test_upscale_false_is_refused_as_a_contradiction():
    with pytest.raises(ValueError, match="derived"):
        validate_request(_upscale_body(harmony_requires={"upscale": False}))


def test_upscale_cannot_be_depth_guided():
    with pytest.raises(ValueError, match="depth-guided"):
        validate_request(_upscale_body(harmony_requires={"depth_guide": True}))


def test_the_input_side_is_bounded():
    with pytest.raises(ValueError, match=f"between 16 and {MAX_UPSCALE_SIDE}"):
        validate_request({"init_image": _image(_png(MAX_UPSCALE_SIDE + 16, 64)), "scale": 2})


@pytest.mark.parametrize("size,scale", [((4096, 4096), 4), ((1440, 3197), 4), ((4096, 2048), 3)])
def test_the_output_size_is_bounded(size, scale):
    with pytest.raises(ValueError, match="output limit"):
        validate_request({"init_image": _image(_png(*size)), "scale": scale})


def test_a_tall_photo_at_two_times_still_fits():
    """The conference photos are 1440x3197: 2x is 2880x6394, which must be accepted."""
    out = validate_request({"init_image": _image(_png(1440, 3197)), "scale": 2})
    assert (out["width"], out["height"]) == (2880, 6394)


def test_the_init_image_is_checked_like_a_control_image():
    body = _upscale_body()
    body["init_image"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="init_image SHA-256 mismatch"):
        validate_request(body)
    body = _upscale_body()
    body["init_image"]["b64_json"] = "not base64!"
    with pytest.raises(ValueError, match="init_image"):
        validate_request(body)


def test_the_init_image_bytes_are_bounded():
    data = b"x" * (MAX_UPSCALE_IMAGE_BYTES + 1)
    with pytest.raises(ValueError, match="MiB"):
        validate_request({"init_image": _image(data), "scale": 2})


@pytest.mark.parametrize("key,value", [("width", 2881), ("height", 1299)])
def test_a_stated_size_must_match_the_derived_one(key, value):
    with pytest.raises(ValueError, match="derived"):
        validate_request(_upscale_body(**{key: value}))


def test_a_stated_matching_size_is_accepted():
    out = validate_request(_upscale_body(width=2880, height=1300))
    assert (out["width"], out["height"]) == (2880, 1300)


def _world():
    return WorldState(units={
        "qwen_image_2512": Unit(kind="qwen_image_2512", footprint={},
                                attributes={"class": "imagegen", "task": "text_to_image", "params_b": 20}),
        "upscale_realesrgan": Unit(kind="upscale_realesrgan", footprint={},
                                   attributes={"class": "imagegen", "task": "upscale", "upscale": True,
                                               "params_b": 0.017}),
    }, devices=())


def test_generic_imagegen_demand_does_not_reach_the_upscaler():
    """`upscale` is specialist-only: a request that does not name it never lands on a unit that
    cannot generate."""
    generic = candidate_kinds(_world(), Request(id="g", kind="", requires={"class": "imagegen"}))
    assert generic == ["qwen_image_2512"]
    upscale = validate_request(_upscale_body())["harmony_requires"]
    assert candidate_kinds(_world(), Request(id="u", kind="", requires=upscale)) == ["upscale_realesrgan"]


def _upscale_config(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret")
    return {
        "token_file": str(token), "params_b": 0.017,
        "model": "xinntao/Real-ESRGAN-x4plus", "unit": "upscale_realesrgan",
        "resident_bytes": 2_000_000_000, "host_id": "node-a",
        "port": 8212, "device_id": "node-a/a46c4c2e", "broker": "http://broker",
        "snapshot": str(tmp_path / "weights"), "scale": 4, "tile": 512,
    }


class _Runtime:
    def __init__(self, request_sink):
        self.sink = request_sink

    def generate(self, request, cancel=None):
        self.sink.append(request)
        size = (request["width"], request["height"])
        return (Image.new("RGB", size, (1, 2, 3)),
                {"task": "upscale", "upscale": {"scale": request["scale"], "network_scale": 4,
                                                "tile": 512, "tiles": 1,
                                                "input_size": request["init_size"],
                                                "output_size": list(size)}})

    def close(self):
        pass


class _Manager:
    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, unit_name, thunk):
        return thunk(self.runtime)


class _Coordinator:
    def acquire_lease(self, *args, **kwargs):
        return {"lease_id": "lease-1"}

    def release_lease(self, lease_id):
        pass


def _client(tmp_path, monkeypatch):
    captured, sink = {}, []
    runtime = _Runtime(sink)

    def fake_attach(app, **kwargs):
        captured.update(kwargs)
        return _Manager(runtime), _Coordinator()

    monkeypatch.setattr(worker, "attach", fake_attach)
    monkeypatch.setattr(worker, "admit", lambda *a, **k: {"granted": True,
                                                          "device_id": "node-a/a46c4c2e"})
    from fastapi.testclient import TestClient
    client = TestClient(worker.create_app(_upscale_config(tmp_path),
                                          runtime_factory=lambda *a, **k: runtime))
    return client, captured, sink


def test_an_upscale_worker_advertises_a_specialist_unit(tmp_path, monkeypatch):
    _, captured, _ = _client(tmp_path, monkeypatch)
    attributes = captured["units"]["upscale_realesrgan"].attributes
    assert attributes["task"] == "upscale"
    assert attributes["upscale"] is True
    assert "steps" not in attributes


def test_an_upscale_worker_serves_an_upscale_request(tmp_path, monkeypatch):
    client, _, sink = _client(tmp_path, monkeypatch)
    response = client.post("/v1/images/generations", json=_upscale_body(),
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    harmony = response.json()["harmony"]
    assert (harmony["width"], harmony["height"]) == (2880, 1300)
    assert harmony["upscale"]["scale"] == 2
    assert harmony["task"] == "upscale"
    assert sink and sink[0]["init_size"] == [1440, 650]


def test_an_upscale_worker_refuses_a_generation_request(tmp_path, monkeypatch):
    client, _, sink = _client(tmp_path, monkeypatch)
    response = client.post("/v1/images/generations", json={"prompt": "a scene"},
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 422
    assert not sink


def test_the_worker_refuses_a_scale_it_is_not_qualified_for(tmp_path, monkeypatch):
    client, _, sink = _client(tmp_path, monkeypatch)
    response = client.post("/v1/images/generations", json=_upscale_body(scale=5),
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 400
    assert not sink
