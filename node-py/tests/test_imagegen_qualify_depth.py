import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image


SCRIPT = Path(__file__).parents[1] / "examples" / "harmony-image" / "qualify-depth.py"
SPEC = importlib.util.spec_from_file_location("imagegen_qualify_depth", SCRIPT)
qualify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualify)


def _png(size=(16, 16), blank=False):
    image = Image.new("RGB", size, (20, 40, 60))
    if not blank:
        for x in range(size[0]):
            image.putpixel((x, 0), (x * 13 % 256, 0, 0))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _request(tmp_path):
    guide = tmp_path / "guide.png"
    guide.write_bytes(_png())
    request = {
        "prompt": "x", "width": 16, "height": 16, "seed": 1,
        "controlnet_conditioning_scale": 1.0,
        "harmony_requires": {
            "class": "imagegen", "task": "text_to_image", "depth_guide": True,
            "model": "m", "revision": "1" * 40, "quantization": "int8",
            "controlnet_model": "c", "controlnet_revision": "2" * 40,
            "controlnet_config_sha256": "3" * 64,
        },
    }
    return qualify.attach_guide(request, guide)


def _response(request, png=None):
    requires = request["harmony_requires"]
    lease = "device-lease-1"
    return {"data": [{"b64_json": base64.b64encode(png or _png()).decode()}], "harmony": {
        "model": "m", "revision": "1" * 40, "quantization": "int8",
        "depth_guide": {
            "image_sha256": request["control_image"]["sha256"], "conditioning_scale": 1.0,
            "controlnet_model": "c", "controlnet_revision": requires["controlnet_revision"],
            "controlnet_config_sha256": requires["controlnet_config_sha256"],
        },
        "fleet_grant": {"granted": True}, "local_grant": {"granted": True},
        "device_leases": [{"lease_id": lease, "device_id": "d"}],
        "device_grants": [{"lease_id": lease, "device_id": "d", "state": "exclusive"}],
    }}


def test_the_client_has_no_lifecycle_to_start_its_own_broker_or_worker():
    source = SCRIPT.read_text()
    for forbidden in ("subprocess", "livestack_node.hostd", "LIVESTACK_REGISTER", "Popen"):
        assert forbidden not in source
    with pytest.raises(SystemExit):
        qualify.main(["up", "--config", "x", "--output", "y"])


def test_guide_is_attached_with_its_digest(tmp_path):
    request = _request(tmp_path)
    data = base64.b64decode(request["control_image"]["b64_json"])
    assert request["control_image"]["sha256"] == hashlib.sha256(data).hexdigest()


def test_verified_response_writes_png_and_receipt(tmp_path, monkeypatch):
    request = _request(tmp_path)
    (tmp_path / "req.json").write_text(json.dumps(request))
    (tmp_path / "token").write_text("secret\n")
    seen = {}

    def post(ingress, token, body, timeout):
        seen.update(ingress=ingress, token=token)
        return _response(body)

    monkeypatch.setattr(qualify, "post", post)
    out = tmp_path / "out" / "image.png"
    assert qualify.main(["--request", str(tmp_path / "req.json"), "--out", str(out),
                         "--token-file", str(tmp_path / "token")]) == 0
    assert seen == {"ingress": qualify.INGRESS, "token": "secret"}
    receipt = json.loads(out.with_suffix(".receipt.json").read_text())
    assert receipt["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert "control_image" not in receipt["request"]


@pytest.mark.parametrize("mutate", [
    lambda r: r["harmony"].pop("device_leases"),
    lambda r: r["harmony"]["device_grants"][0].update(state="draining"),
    lambda r: r["harmony"]["depth_guide"].update(image_sha256="0" * 64),
    lambda r: r["harmony"].update(revision="9" * 40),
    lambda r: r["harmony"]["local_grant"].update(degraded="peer down"),
    lambda r: r["data"][0].update(b64_json=base64.b64encode(_png(blank=True)).decode()),
    lambda r: r["data"][0].update(b64_json=base64.b64encode(_png(size=(32, 16))).decode()),
])
def test_unmanaged_or_mismatched_results_are_refused(tmp_path, mutate):
    request = _request(tmp_path)
    response = _response(request)
    mutate(response)
    with pytest.raises(qualify.QualificationError):
        qualify.verify(request, response)
