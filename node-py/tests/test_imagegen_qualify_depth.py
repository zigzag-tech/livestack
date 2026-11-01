import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image


SCRIPT = Path(__file__).parents[1] / "examples" / "harmony-image" / "qualify-depth.py"
SPEC = importlib.util.spec_from_file_location("imagegen_qualify_depth", SCRIPT)
qualify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualify)


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _png(size=(16, 16), blank=False):
    image = Image.new("RGB", size, (20, 40, 60))
    if not blank:
        for x in range(size[0]):
            for y in range(size[1]):
                image.putpixel((x, y), ((x * 17) % 256, (y * 29) % 256, ((x + y) * 11) % 256))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _request(dispatch):
    guide = _png()
    return {
        "prompt": "An explicit grounded depth qualification scene",
        "width": 16,
        "height": 16,
        "seed": 1863,
        "control_image": {
            "media_type": "image/png",
            "b64_json": base64.b64encode(guide).decode("ascii"),
            "sha256": hashlib.sha256(guide).hexdigest(),
        },
        "controlnet_conditioning_scale": 0.8,
        "harmony_requires": {
            "class": "imagegen",
            "task": "text_to_image",
            "depth_guide": True,
            "model": qualify.BASE_MODEL,
            "revision": qualify.BASE_REVISION,
            "controlnet_model": qualify.CONTROLNET_MODEL,
            "controlnet_revision": qualify.CONTROLNET_REVISION,
            "controlnet_config_sha256": qualify.CONTROLNET_CONFIG_SHA256,
            "quantization": dispatch["expected"]["quantization"],
            "params_b>=": 20,
        },
    }


def _response(dispatch, request):
    expected = dispatch["expected"]
    grant = {"granted": True, "kind": expected["unit"], "device_id": expected["device_id"]}
    return {
        "created": 123,
        "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
        "harmony": {
            "model": qualify.BASE_MODEL,
            "revision": qualify.BASE_REVISION,
            "unit": expected["unit"],
            "host": expected["host"],
            "device_id": expected["device_id"],
            "device_ids": expected["device_ids"],
            "prompt": request["prompt"],
            "seed": request["seed"],
            "width": request["width"],
            "height": request["height"],
            "requirements": request["harmony_requires"],
            "quantization": expected["quantization"],
            "steps": 50,
            "worker_endpoint": dispatch["facade_url"][:-len("/livestack")],
            "depth_guide": {
                "image_sha256": request["control_image"]["sha256"],
                "conditioning_scale": request["controlnet_conditioning_scale"],
                "controlnet_model": qualify.CONTROLNET_MODEL,
                "controlnet_revision": qualify.CONTROLNET_REVISION,
                "controlnet_config_sha256": qualify.CONTROLNET_CONFIG_SHA256,
                "controlnet_quantization": dispatch["controlnet"]["quantization"],
                "control_guidance_start": 0.0,
                "control_guidance_end": 1.0,
            },
            "generation_settings": dict(qualify.GENERATION_SETTINGS),
            "fleet_grant": dict(grant, lease_id="fleet-lease"),
            "local_grant": dict(grant, lease_id="local-lease"),
        },
    }


class FakeProcess:
    def __init__(self, harness, role, argv, kwargs):
        self.harness = harness
        self.role = role
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = harness.next_pid
        harness.next_pid += 1
        self.returncode = None
        self.env = dict(kwargs["env"])
        self.directory = harness.proc_root / str(self.pid)
        self.directory.mkdir(parents=True)
        (self.directory / "cmdline").write_bytes(b"\0".join(os.fsencode(part) for part in argv) + b"\0")
        (self.directory / "environ").write_bytes(
            b"\0".join(os.fsencode(key) + b"=" + os.fsencode(value) for key, value in self.env.items()) + b"\0")
        (self.directory / "stat").write_text(
            f"{self.pid} (python) " + " ".join(
                str(100000 + self.pid) if index == 19 else "S" if index == 0 else "0"
                for index in range(24)) + "\n")
        (self.directory / "exe").symlink_to(os.path.realpath(sys.executable))

    def poll(self):
        return self.returncode


class FakeLifecycle:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.proc_root = tmp_path / "proc"
        self.proc_root.mkdir()
        self.next_pid = 4000
        self.processes = {}
        self.role_processes = {}
        self.spawned = []
        self.signals = []
        self.calls = []
        self.ports = (19101, 19102, 19103)
        self.fleet_mutator = None
        self.post_response = None
        self.ignore_term = set()
        self.wrapper = tmp_path / "owner-wrapper.py"
        self.wrapper.write_text("# owner qualification wrapper\n")
        self.config = self._owner_config()
        self.config_path = tmp_path / "owner-variant.json"
        self.handoff_path = tmp_path / "handoff.json"
        monkeypatch.setattr(qualify, "PROC_ROOT", self.proc_root)
        monkeypatch.setattr(qualify, "_package_version", lambda name: {"torch": "2.9.1", "diffusers": "0.36.0"}[name])
        monkeypatch.setattr(qualify, "_git_revision", lambda root: "f" * 40)
        monkeypatch.setattr(qualify, "_allocate_ports", lambda count=3: list(self.ports)[:count])
        monkeypatch.setattr(qualify.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(qualify.os, "killpg", self.killpg)
        monkeypatch.setattr(qualify.subprocess, "Popen", self.popen)
        monkeypatch.setattr(qualify, "_http_json", self.http)

    @property
    def broker_url(self):
        return f"http://127.0.0.1:{self.ports[0]}"

    @property
    def facade_url(self):
        return f"http://127.0.0.1:{self.ports[1]}/livestack"

    @property
    def gateway_url(self):
        return f"http://127.0.0.1:{self.ports[2]}"

    def _owner_config(self):
        return {
            "unit": "qwen_image_2512_depth_nf4",
            "host_id": "owner-host",
            "device_id": "owner-host/gpu1",
            "cuda_visible_devices": "1",
            "broker": "http://127.0.0.1:18799",
            "port": 18215,
            "token_file": "/home/ubuntu/harmony-image/worker.token",
            "model": qualify.BASE_MODEL,
            "revision": qualify.BASE_REVISION,
            "snapshot": str(self.tmp_path / "base" / qualify.BASE_REVISION),
            "params_b": 20,
            "resident_bytes": 20500000000,
            "quantization": "nf4",
            "steps": 50,
            "true_cfg_scale": 4.0,
            "negative_prompt": "",
            "max_sequence_length": 512,
            "cpu_threads": 12,
            "idle_seconds": 900,
            "max_dimension": 1024,
            "depth_guide": {
                "model": qualify.CONTROLNET_MODEL,
                "revision": qualify.CONTROLNET_REVISION,
                "snapshot": str(self.tmp_path / "control" / qualify.CONTROLNET_REVISION),
                "config_sha256": qualify.CONTROLNET_CONFIG_SHA256,
                "weight_sha256": qualify.CONTROLNET_WEIGHT_SHA256,
                "weight_bytes": qualify.CONTROLNET_WEIGHT_BYTES,
            },
            "wrapper": str(self.wrapper),
            "run_root": str(self.tmp_path / "runs"),
            "startup_timeout_s": 1,
            "shutdown_timeout_s": 1,
        }

    def save_config(self):
        _write_json(self.config_path, self.config)

    def up(self):
        self.save_config()
        return qualify.up(self.config_path, self.handoff_path)

    def popen(self, argv, **kwargs):
        role = {module: name for name, module in qualify.MODULES.items()}[argv[2]]
        process = FakeProcess(self, role, argv, kwargs)
        self.processes[process.pid] = process
        self.role_processes[role] = process
        self.spawned.append(process)
        return process

    def killpg(self, pgid, sig):
        self.signals.append((pgid, sig))
        process = self.processes.get(pgid)
        if process is None:
            raise ProcessLookupError(pgid)
        if sig == signal.SIGTERM and process.role in self.ignore_term:
            return
        process.returncode = -sig
        shutil.rmtree(process.directory)

    def attributes(self):
        attributes = {
            "class": "imagegen",
            "task": "text_to_image",
            "model": qualify.BASE_MODEL,
            "revision": qualify.BASE_REVISION,
            "quantization": self.config["quantization"],
            **qualify._generation_settings(self.config),
        }
        if "depth_guide" in self.config:
            attributes.update({
                "depth_guide": True,
                "controlnet_model": qualify.CONTROLNET_MODEL,
                "controlnet_revision": qualify.CONTROLNET_REVISION,
                "controlnet_config_sha256": qualify.CONTROLNET_CONFIG_SHA256,
                "controlnet_quantization": self.config["depth_guide"].get("quantization", "bf16"),
            })
        return attributes

    def fleet(self):
        view = {
            "dispatch": True,
            "peers": 1,
            "hosts": {
                self.config["host_id"]: {
                    "nodes": [{
                        "peer": self.facade_url,
                        "state": "fresh",
                        "ready": False,
                        "device_id": self.config["device_id"],
                        "units": [{"kind": self.config["unit"], "attributes": self.attributes()}],
                    }],
                },
            },
        }
        nodes = view["hosts"][self.config["host_id"]]["nodes"]
        for device_id in self.config.get("device_ids", [])[1:]:
            nodes.append({"peer": "local-meter://" + device_id, "state": "fresh",
                          "device_id": device_id, "units": []})
        view["peers"] = len(nodes)
        if self.fleet_mutator:
            self.fleet_mutator(view)
        return view

    def http(self, base, method, path, body=None, token=None, timeout=30):
        self.calls.append((base, method, path, body, token))
        if base == self.broker_url and path == "/peers":
            hostd = self.role_processes.get("hostd")
            return {
                "host_id": hostd.env["LIVESTACK_HOST_ID"],
                "peers": [{
                    "peer": self.facade_url,
                    "host_id": self.config["host_id"],
                    "state": "fresh" if "worker" in self.role_processes else "suspect",
                }] + [{"peer": "local-meter://" + device_id, "state": "fresh"}
                      for device_id in self.config.get("device_ids", [])[1:]],
            }
        if base == self.facade_url and path == "/health":
            return {"ok": True}
        if base == self.facade_url and path == "/capability":
            return {
                "kind": "imagegen",
                "host_id": self.config["host_id"],
                "device_id": self.config["device_id"],
                "units": [self.config["unit"]],
                "ready": False,
                "detail": "cold",
            }
        if base == self.facade_url and path == "/residence":
            return {
                "host_id": self.config["host_id"],
                "device_id": self.config["device_id"],
                "units": [{"kind": self.config["unit"], "attributes": self.attributes()}],
            }
        if base == self.broker_url and path == "/plan":
            return {"plan": []}
        if base == self.broker_url and path == "/fleet":
            return self.fleet()
        if base == self.gateway_url and path == "/health":
            return {"ok": True, "fleet": self.broker_url}
        if base == self.gateway_url and path == "/v1/images/generations" and method == "POST":
            return copy.deepcopy(self.post_response)
        raise AssertionError((base, method, path))


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    return FakeLifecycle(tmp_path, monkeypatch)


def test_up_builds_isolated_three_process_handoff(lifecycle, monkeypatch):
    monkeypatch.setenv("LIVESTACK_RELAY_URLS", "http://production-relay.invalid")
    monkeypatch.setenv("ALIYUN_ACCESS_KEY_SECRET", "inherited-secret")
    dispatch = lifecycle.up()

    assert [process.role for process in lifecycle.spawned] == ["hostd", "worker", "gateway"]
    assert dispatch["schema"] == "livestack.image-depth-dispatch.v1"
    assert dispatch["controlnet"]["quantization"] == "bf16"
    assert stat.S_IMODE(Path(dispatch["run_dir"]).stat().st_mode) == 0o700
    assert dispatch["expected"] == {
        "host": "owner-host",
        "device_id": "owner-host/gpu1",
        "device_ids": ["owner-host/gpu1"],
        "unit": "qwen_image_2512_depth_nf4",
        "quantization": "nf4",
        "depth_guide": True,
        "cuda_visible_devices": "1",
        "logical_device": "cuda:0",
    }
    hostd = lifecycle.role_processes["hostd"]
    worker = lifecycle.role_processes["worker"]
    gateway = lifecycle.role_processes["gateway"]
    assert hostd.env["LIVESTACK_DISPATCH"] == "apply"
    assert hostd.env["LIVESTACK_PEERS"] == lifecycle.facade_url
    assert worker.env["LIVESTACK_REGISTER"] == "0"
    assert worker.env["CUDA_VISIBLE_DEVICES"] == "1"
    for process in lifecycle.spawned:
        env = process.env
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["HARMONY_STREAMS"] == "0"
        assert env["LIVESTACK_MESH_ENABLED"] == "0"
        assert env["LIVESTACK_LEDGER"] == "0"
        assert env["LIVESTACK_LEDGER_DIR"].startswith(dispatch["run_dir"])
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "LIVESTACK_RELAY_URLS" not in env
        assert "ALIYUN_ACCESS_KEY_SECRET" not in env
        assert process.kwargs["stdin"] == subprocess.DEVNULL
        assert process.kwargs["stderr"] == subprocess.STDOUT
        assert process.kwargs["close_fds"] is True
        assert process.kwargs["start_new_session"] is True
        assert process.kwargs["stdout"] is not subprocess.PIPE
    worker_config = json.loads(Path(dispatch["configs"]["worker"]["path"]).read_text())
    gateway_config = json.loads(Path(dispatch["configs"]["gateway"]["path"]).read_text())
    assert worker_config["broker"] == gateway_config["fleet"] == lifecycle.broker_url
    assert worker_config["token_file"] == dispatch["token_files"]["worker"]
    assert worker_config["fleet_token_file"] == dispatch["token_files"]["worker"]
    assert gateway_config["fleet_token_file"] == dispatch["token_files"]["fleet"]
    assert worker_config["token_file"] != "/home/ubuntu/harmony-image/worker.token"
    worker_token = Path(dispatch["token_files"]["worker"]).read_text().strip()
    fleet_token = Path(dispatch["token_files"]["fleet"]).read_text().strip()
    assert worker_token != fleet_token
    assert not worker_token.startswith("{")
    assert not fleet_token.startswith("{")
    assert stat.S_IMODE(Path(dispatch["token_files"]["worker"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(dispatch["token_files"]["fleet"]).stat().st_mode) == 0o600
    node_principals = json.loads(Path(dispatch["token_files"]["node_principals"]).read_text())
    fleet_principals = json.loads(Path(dispatch["token_files"]["fleet_principals"]).read_text())
    assert node_principals[worker_token]["name"] == "depth-qualification-hostd"
    assert fleet_principals[fleet_token]["owner"] == gateway_config["owner"]
    handoff_text = lifecycle.handoff_path.read_text()
    assert worker_token not in handoff_text
    assert fleet_token not in handoff_text
    assert "inherited-secret" not in handoff_text
    assert ":8799" not in handoff_text
    assert ":8211" not in handoff_text
    assert dispatch["wrapper"]["path"] != str(qualify.PRODUCTION_WRAPPER)
    assert hashlib.sha256(Path(dispatch["wrapper"]["path"]).read_bytes()).hexdigest() == dispatch["wrapper"]["sha256"]
    paths = [call[2] for call in lifecycle.calls]
    assert paths[0] == "/peers"
    assert paths.index("/plan") < paths.index("/fleet") < len(paths) - 1
    assert paths[-1] == "/health"
    assert dispatch["processes"]["worker"]["start_ticks"] > 0
    assert dispatch["processes"]["gateway"]["argv"][1:] == ["-m", "livestack_node.imagegen.gateway"]


@pytest.mark.parametrize("key,value", [
    ("broker", "http://127.0.0.1:8799"),
    ("gateway", "http://100.64.0.18:8211"),
    ("broker", "http://100.64.0.18:18799"),
    ("wrapper", str(qualify.PRODUCTION_WRAPPER)),
])
def test_up_refuses_production_or_nonisolated_routes_before_spawn(lifecycle, key, value):
    lifecycle.config[key] = value
    lifecycle.save_config()
    with pytest.raises(qualify.QualificationError, match="production|isolated|loopback|wrapper"):
        qualify.up(lifecycle.config_path, lifecycle.handoff_path)
    assert lifecycle.spawned == []
    assert lifecycle.calls == []


def test_mixed_kind_collision_shuts_down_only_recorded_processes(lifecycle):
    def collide(view):
        node = view["hosts"]["owner-host"]["nodes"][0]
        node["units"].append({"kind": "qwen_image_2512", "attributes": {"class": "imagegen"}})
    lifecycle.fleet_mutator = collide
    with pytest.raises(qualify.QualificationError, match="collision"):
        lifecycle.up()
    assert [process.role for process in lifecycle.spawned] == ["hostd", "worker"]
    assert lifecycle.signals == [
        (lifecycle.role_processes["worker"].pid, signal.SIGTERM),
        (lifecycle.role_processes["hostd"].pid, signal.SIGTERM),
    ]


@pytest.mark.parametrize("unit,quantization", [
    ("qwen_image_2512_depth_nf4", "int8"),
    ("qwen_image_2512_depth_int8", "nf4"),
])
def test_both_crossed_pairs_are_refused_before_spawn(lifecycle, unit, quantization):
    lifecycle.config["unit"] = unit
    lifecycle.config["quantization"] = quantization
    lifecycle.save_config()
    with pytest.raises(qualify.QualificationError, match="crossed"):
        qualify.up(lifecycle.config_path, lifecycle.handoff_path)
    assert lifecycle.spawned == []


@pytest.mark.parametrize("quantization", ["nf4", "int4", None, True])
def test_invalid_controlnet_quantization_is_refused_before_spawn(lifecycle, quantization):
    lifecycle.config["depth_guide"]["quantization"] = quantization
    with pytest.raises(qualify.QualificationError, match="depth_guide.quantization"):
        lifecycle.up()
    assert lifecycle.spawned == []


def test_int8_controlnet_is_preserved_and_verified_through_smoke(lifecycle):
    lifecycle.config["unit"] = "qwen_image_2512_depth_int8"
    lifecycle.config["quantization"] = "int8"
    lifecycle.config["depth_guide"]["quantization"] = "int8"
    dispatch = lifecycle.up()

    assert dispatch["controlnet"]["quantization"] == "int8"
    worker = json.loads(Path(dispatch["configs"]["worker"]["path"]).read_text())
    assert worker["depth_guide"]["quantization"] == "int8"
    attributes = lifecycle.fleet()["hosts"]["owner-host"]["nodes"][0]["units"][0]["attributes"]
    assert attributes["controlnet_quantization"] == "int8"

    request = _request(dispatch)
    assert "controlnet_quantization" not in request["harmony_requires"]
    response = _response(dispatch, request)
    lifecycle.post_response = response
    request_path = lifecycle.tmp_path / "request-int8.json"
    _write_json(request_path, request)
    summary = qualify.smoke(lifecycle.handoff_path, request_path)
    assert summary["depth_guide"]["controlnet_quantization"] == "int8"


def test_text_int8_up_has_no_depth_or_controlnet_configuration(lifecycle):
    lifecycle.config.pop("depth_guide")
    lifecycle.config["unit"] = "qwen_image_2512_text_int8"
    lifecycle.config["quantization"] = "int8"
    lifecycle.config["negative_prompt"] = qualify.QWEN_2512_NEGATIVE_PROMPT

    dispatch = lifecycle.up()

    assert [process.role for process in lifecycle.spawned] == ["hostd", "worker", "gateway"]
    assert dispatch["expected"] == {
        "host": "owner-host",
        "device_id": "owner-host/gpu1",
        "device_ids": ["owner-host/gpu1"],
        "unit": "qwen_image_2512_text_int8",
        "quantization": "int8",
        "depth_guide": False,
        "cuda_visible_devices": "1",
        "logical_device": "cuda:0",
    }
    assert dispatch["controlnet"] is None
    assert dispatch["generation_settings"] == {
        **qualify.GENERATION_SETTINGS,
        "negative_prompt": qualify.QWEN_2512_NEGATIVE_PROMPT,
    }
    attributes = lifecycle.fleet()["hosts"]["owner-host"]["nodes"][0]["units"][0]["attributes"]
    assert "depth_guide" not in attributes
    assert not any(key == "controlnet" or key.startswith("controlnet_") for key in attributes)
    worker = json.loads(Path(dispatch["configs"]["worker"]["path"]).read_text())
    assert "depth_guide" not in worker
    assert not any(key == "controlnet" or key.startswith("controlnet_") for key in worker)
    assert worker["negative_prompt"] == qualify.QWEN_2512_NEGATIVE_PROMPT


@pytest.mark.parametrize("unit,quantization", [
    ("qwen_image_2512_text_nf4", "int8"),
    ("qwen_image_2512_text_int8", "nf4"),
])
def test_crossed_text_pairs_are_refused_before_spawn(lifecycle, unit, quantization):
    lifecycle.config.pop("depth_guide")
    lifecycle.config["unit"] = unit
    lifecycle.config["quantization"] = quantization
    lifecycle.save_config()
    with pytest.raises(qualify.QualificationError, match="crossed"):
        qualify.up(lifecycle.config_path, lifecycle.handoff_path)
    assert lifecycle.spawned == []
    assert lifecycle.calls == []


@pytest.mark.parametrize("wrong", ["revision", "snapshot"])
def test_wrong_base_snapshot_or_revision_is_refused(lifecycle, wrong):
    if wrong == "revision":
        lifecycle.config["revision"] = "0" * 40
        lifecycle.config["snapshot"] = str(lifecycle.tmp_path / "base" / ("0" * 40))
    else:
        lifecycle.config["snapshot"] = str(lifecycle.tmp_path / "base" / "wrong-snapshot")
    lifecycle.save_config()
    with pytest.raises(qualify.QualificationError, match="base|snapshot|revision"):
        qualify.up(lifecycle.config_path, lifecycle.handoff_path)
    assert lifecycle.spawned == []


def test_official_qwen_negative_prompt_is_pinned_through_dispatch(lifecycle):
    lifecycle.config["negative_prompt"] = qualify.QWEN_2512_NEGATIVE_PROMPT
    lifecycle.config["max_dimension"] = 1664
    dispatch = lifecycle.up()
    assert dispatch["generation_settings"]["negative_prompt"] == qualify.QWEN_2512_NEGATIVE_PROMPT
    worker = json.loads(Path(dispatch["configs"]["worker"]["path"]).read_text())
    assert worker["negative_prompt"] == qualify.QWEN_2512_NEGATIVE_PROMPT
    assert worker["max_dimension"] == 1664


def test_fleet_and_facade_require_expected_unit_and_device(lifecycle):
    dispatch = lifecycle.up()
    view = lifecycle.fleet()
    wrong_device = copy.deepcopy(view)
    wrong_device["hosts"]["owner-host"]["nodes"][0]["device_id"] = "owner-host/gpu0"
    with pytest.raises(qualify.QualificationError, match="wrong device"):
        qualify._validate_fleet(wrong_device, dispatch)
    wrong_unit = copy.deepcopy(view)
    wrong_unit["hosts"]["owner-host"]["nodes"][0]["units"][0]["kind"] = "qwen_image_2512"
    with pytest.raises(qualify.QualificationError, match="wrong expected unit"):
        qualify._validate_fleet(wrong_unit, dispatch)
    capability = {
        "kind": "imagegen",
        "host_id": "owner-host",
        "device_id": "owner-host/gpu1",
        "units": ["qwen_image_2512"],
        "ready": False,
    }
    with pytest.raises(qualify.QualificationError, match="exactly the expected unit"):
        qualify._validate_capability(capability, dispatch)


def test_smoke_decodes_actual_png_and_retains_verified_evidence(lifecycle):
    dispatch = lifecycle.up()
    request = _request(dispatch)
    request_path = lifecycle.tmp_path / "request.json"
    _write_json(request_path, request)
    lifecycle.post_response = _response(dispatch, request)

    summary = qualify.smoke(lifecycle.handoff_path, request_path)

    output = Path(summary["output"]["path"])
    assert output == Path(dispatch["run_dir"]) / "smoke.png"
    assert output.read_bytes() == base64.b64decode(lifecycle.post_response["data"][0]["b64_json"])
    assert hashlib.sha256(output.read_bytes()).hexdigest() == summary["output"]["sha256"]
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (16, 16)
    assert summary["output"]["nonblank"] is True
    assert summary["generation_settings"] == qualify.GENERATION_SETTINGS
    assert summary["depth_guide"]["image_sha256"] == request["control_image"]["sha256"]
    assert (output.parent / "smoke.json").is_file()
    assert (output.parent / "smoke-request.json").read_bytes() == request_path.read_bytes()
    assert (output.parent / "smoke-response.json").is_file()
    post = next(call for call in lifecycle.calls if call[2] == "/v1/images/generations")
    assert post[0] == lifecycle.gateway_url
    assert post[3] == request
    expected_token = Path(dispatch["token_files"]["worker"]).read_text().strip()
    assert secrets.compare_digest(post[4], expected_token)
    assert expected_token not in (output.parent / "smoke.json").read_text()


@pytest.mark.parametrize("mutation", [
    "wrong-guide-hash", "wrong-scale", "wrong-config", "wrong-controlnet-quantization",
    "wrong-interval", "wrong-steps", "degraded-local-grant", "wrong-fleet-kind",
    "wrong-harmony-width", "wrong-png-size", "blank-png",
])
def test_actual_png_and_every_evidence_field_are_verified(lifecycle, mutation):
    dispatch = lifecycle.up()
    request = _request(dispatch)
    response = _response(dispatch, request)
    harmony = response["harmony"]
    if mutation == "wrong-guide-hash":
        harmony["depth_guide"]["image_sha256"] = "0" * 64
    elif mutation == "wrong-scale":
        harmony["depth_guide"]["conditioning_scale"] = 0.9
    elif mutation == "wrong-config":
        harmony["depth_guide"]["controlnet_config_sha256"] = "0" * 64
    elif mutation == "wrong-controlnet-quantization":
        harmony["depth_guide"]["controlnet_quantization"] = "int8"
    elif mutation == "wrong-interval":
        harmony["depth_guide"]["control_guidance_end"] = 0.9
    elif mutation == "wrong-steps":
        harmony["generation_settings"]["steps"] = 49
    elif mutation == "degraded-local-grant":
        harmony["local_grant"]["degraded"] = "broker unavailable"
    elif mutation == "wrong-fleet-kind":
        harmony["fleet_grant"]["kind"] = "qwen_image_2512"
    elif mutation == "wrong-harmony-width":
        harmony["width"] = 1024
    elif mutation == "wrong-png-size":
        response["data"][0]["b64_json"] = base64.b64encode(_png(size=(8, 16))).decode("ascii")
    elif mutation == "blank-png":
        response["data"][0]["b64_json"] = base64.b64encode(_png(blank=True)).decode("ascii")
    with pytest.raises(qualify.QualificationError):
        qualify._verify_smoke_response(response, request, dispatch)


def test_environment_fingerprint_is_stable_across_pids_ports_and_token_paths(lifecycle):
    first = lifecycle.up()
    lifecycle.ports = (19201, 19202, 19203)
    lifecycle.handoff_path = lifecycle.tmp_path / "handoff-second.json"
    second = lifecycle.up()

    assert first["run_id"] != second["run_id"]
    assert first["broker_url"] != second["broker_url"]
    assert first["processes"]["hostd"]["pid"] != second["processes"]["hostd"]["pid"]
    assert first["token_files"]["worker"] != second["token_files"]["worker"]
    assert first["configs"]["worker"]["sha256"] != second["configs"]["worker"]["sha256"]
    assert first["environment_fingerprint"] == second["environment_fingerprint"]
    assert qualify._fingerprint_material(first) == qualify._fingerprint_material(second)
    material = qualify._fingerprint_material(second)
    assert "processes" not in material
    assert "token_files" not in material
    assert "broker_url" not in material
    assert "facade_url" not in material
    assert "run_dir" not in material


def test_down_verifies_and_stops_gateway_worker_hostd_in_order(lifecycle):
    dispatch = lifecycle.up()
    result = qualify.down(lifecycle.handoff_path)
    assert result == {"stopped": ["gateway", "worker", "hostd"]}
    assert lifecycle.signals == [
        (dispatch["processes"]["gateway"]["pid"], signal.SIGTERM),
        (dispatch["processes"]["worker"]["pid"], signal.SIGTERM),
        (dispatch["processes"]["hostd"]["pid"], signal.SIGTERM),
    ]
    qualify.down(lifecycle.handoff_path)
    assert len(lifecycle.signals) == 3


@pytest.mark.parametrize("mutation", ["argv", "start_ticks", "executable", "environment", "config", "pid-reuse"])
def test_down_refuses_pid_command_environment_or_config_mismatch(lifecycle, mutation):
    dispatch = lifecycle.up()
    if mutation == "config":
        with Path(dispatch["configs"]["worker"]["path"]).open("a") as handle:
            handle.write(" ")
    else:
        edited = copy.deepcopy(dispatch)
        record = edited["processes"]["worker"]
        if mutation == "argv":
            record["argv"][2] = "livestack_node.imagegen.gateway"
        elif mutation == "start_ticks":
            record["start_ticks"] += 1
        elif mutation == "executable":
            record["executable"] = "/bin/false"
        elif mutation == "environment":
            record["environment"]["CUDA_VISIBLE_DEVICES"] = "0"
        elif mutation == "pid-reuse":
            replacement = lifecycle.popen([
                sys.executable, "-m", "livestack_node.imagegen.worker",
            ], env=record["environment"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
            record["pid"] = replacement.pid
            record["pgid"] = replacement.pid
        _write_json(lifecycle.handoff_path, edited)
    with pytest.raises(qualify.QualificationError):
        qualify.down(lifecycle.handoff_path)
    assert lifecycle.signals == []


def test_sigkill_requires_revalidation_after_sigterm_deadline(lifecycle):
    lifecycle.ignore_term.add("gateway")
    dispatch = lifecycle.up()
    qualify.down(lifecycle.handoff_path, shutdown_timeout_s=0)
    assert lifecycle.signals == [
        (dispatch["processes"]["gateway"]["pid"], signal.SIGTERM),
        (dispatch["processes"]["gateway"]["pid"], signal.SIGKILL),
        (dispatch["processes"]["worker"]["pid"], signal.SIGTERM),
        (dispatch["processes"]["hostd"]["pid"], signal.SIGTERM),
    ]


def test_staged_revision_and_manifest_are_bound_without_git_metadata(lifecycle):
    lifecycle.config["code_revision"] = "a" * 40
    lifecycle.config["source_manifest_sha256"] = "b" * 64
    lifecycle.monkeypatch.setattr(qualify, "_git_revision", lambda root: (_ for _ in ()).throw(
        AssertionError("Git metadata must not be required for staged runs")))
    dispatch = lifecycle.up()
    assert dispatch["livestack_code_revision"] == "a" * 40
    assert dispatch["source_manifest_sha256"] == "b" * 64
    assert qualify._fingerprint_material(dispatch)["source_manifest_sha256"] == "b" * 64


def test_invalid_staged_revision_is_refused_before_spawn(lifecycle):
    lifecycle.config["code_revision"] = "not-a-full-revision"
    with pytest.raises(qualify.QualificationError, match="code_revision"):
        lifecycle.up()
    assert lifecycle.spawned == []


def test_python_symlink_is_not_resolved_away_from_venv(lifecycle, tmp_path):
    link = tmp_path / "image-venv-python"
    link.symlink_to("/usr/bin/python3")
    lifecycle.monkeypatch.setattr(qualify.sys, "executable", str(link))
    dispatch = lifecycle.up()
    assert dispatch["python"] == str(link)
    assert dispatch["python"] != "/usr/bin/python3"


def test_facade_status_ok_health_shape_is_accepted(monkeypatch):
    monkeypatch.setattr(qualify, "_http_json", lambda *a, **k: {"status": "ok"})
    result = qualify._wait_json(
        "facade health", "http://test", "/health",
        lambda value: value.get("ok") is True or value.get("status") == "ok",
        {}, {}, 0.01)
    assert result == {"status": "ok"}


def test_record_process_waits_for_exec_before_reading_argv(lifecycle):
    argv = [sys.executable, "-m", "livestack_node.imagegen.gateway"]
    process = lifecycle.popen(argv, env={}, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                              close_fds=True, start_new_session=True)
    (process.directory / "cmdline").write_bytes(b"")
    def restore_argv():
        (process.directory / "cmdline").write_bytes(
            b"\0".join(os.fsencode(part) for part in argv) + b"\0")
    timer = threading.Timer(0.05, restore_argv)
    timer.start()
    try:
        record = qualify._record_process("gateway", process, argv,
                                         lifecycle.tmp_path / "gateway.log", {})
    finally:
        timer.join()
    assert record["argv"] == argv


def test_exclusive_up_aligns_gateway_and_worker_lease_owner(lifecycle):
    lifecycle.config["exclusive_device"] = {
        "min_free_bytes": 23_000_000_000,
        "wait_timeout_s": 240,
        "ttl_seconds": 120,
    }
    dispatch = lifecycle.up()
    gateway = json.loads(Path(dispatch["configs"]["gateway"]["path"]).read_text())
    worker = json.loads(Path(dispatch["configs"]["worker"]["path"]).read_text())
    principals = json.loads(Path(dispatch["token_files"]["fleet_principals"]).read_text())
    assert gateway["owner"] == "harmony-image"
    assert gateway["device_id"] == lifecycle.config["device_id"]
    assert gateway["exclusive_device"] == worker["exclusive_device"]
    assert {principal["owner"] for principal in principals.values()} == {"harmony-image"}


def test_multi_device_up_preserves_ordered_cuda_mapping_and_device_capacity(lifecycle):
    lifecycle.config["device_ids"] = ["owner-host/gpu1", "owner-host/gpu0"]
    lifecycle.config["cuda_visible_devices"] = "1,0"
    lifecycle.config["exclusive_device"] = {
        "min_free_bytes": {"owner-host/gpu1": 21_000_000_000,
                           "owner-host/gpu0": 4_000_000_000},
    }
    dispatch = lifecycle.up()
    gateway = json.loads(Path(dispatch["configs"]["gateway"]["path"]).read_text())
    hostd = json.loads(Path(dispatch["configs"]["hostd_launch"]["path"]).read_text())
    assert dispatch["expected"]["device_ids"] == lifecycle.config["device_ids"]
    assert dispatch["expected"]["cuda_visible_devices"] == "1,0"
    assert gateway["device_ids"] == lifecycle.config["device_ids"]
    assert set(json.loads(hostd["environment"]["LIVESTACK_DEVICES"])) == set(lifecycle.config["device_ids"])
    assert dispatch["processes"]["worker"]["environment"]["CUDA_VISIBLE_DEVICES"] == "1,0"
    assert json.loads(hostd["environment"]["LIVESTACK_DEVICE_METERS"]) == {"owner-host/gpu0": 0}
