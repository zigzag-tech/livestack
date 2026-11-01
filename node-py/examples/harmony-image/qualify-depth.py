#!/usr/bin/env python3
"""Isolated managed qualification lifecycle for Qwen image depth guidance."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SCHEMA = "livestack.image-depth-dispatch.v1"
SMOKE_SCHEMA = "livestack.image-depth-smoke.v1"
BASE_MODEL = "Qwen/Qwen-Image-2512"
BASE_REVISION = "25468b98e3276ca6700de15c6628e51b7de54a26"
CONTROLNET_MODEL = "InstantX/Qwen-Image-ControlNet-Union"
CONTROLNET_REVISION = "b13036f066d6dee7c20513e263d3d673055e9de8"
CONTROLNET_CONFIG_SHA256 = "6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd"
CONTROLNET_WEIGHT_SHA256 = "d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0"
CONTROLNET_WEIGHT_BYTES = 3536027816
UNIT_FOR_QUANTIZATION = {
    "nf4": "qwen_image_2512_depth_nf4",
    "int8": "qwen_image_2512_depth_int8",
}
TEXT_UNIT_FOR_QUANTIZATION = {
    "nf4": "qwen_image_2512_text_nf4",
    "int8": "qwen_image_2512_text_int8",
}
GENERATION_SETTINGS = {
    "steps": 50,
    "true_cfg_scale": 4.0,
    "negative_prompt": "",
    "text_encoder": "CPU FP32",
    "max_sequence_length": 512,
}
QWEN_2512_NEGATIVE_PROMPT = "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
SDXL_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
FLUX2_MODEL = "black-forest-labs/FLUX.2-dev"


def _generation_settings(config):
    if config.get("model") in (SDXL_MODEL, FLUX2_MODEL):
        sdxl = config.get("model") == SDXL_MODEL
        return {
            "steps": config.get("steps"),
            "guidance_scale": config.get("guidance_scale", 7.5 if sdxl else 4.0),
            "negative_prompt": config.get("negative_prompt", "blurry, low quality, distorted geometry" if sdxl else ""),
            "text_encoder": "CPU FP32" if sdxl else "CPU INT8",
            **({} if sdxl else {"quantization_backend": config.get(
                "quantization_backend", "bitsandbytes")}),
        }
    return {**GENERATION_SETTINGS, "negative_prompt": config.get("negative_prompt", "")}


PRODUCTION_WRAPPER = Path("/home/ubuntu/harmony-image/generate-creative-image.py")
PRODUCTION_PORTS = {8799, 8211}
MODULES = {
    "hostd": "livestack_node.hostd",
    "worker": "livestack_node.imagegen.worker",
    "gateway": "livestack_node.imagegen.gateway",
}
STOP_ORDER = ("gateway", "worker", "hostd")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RUN_ID_RE = re.compile(r"[0-9a-f]{16,32}")
SECRET_KEYS = {
    "token", "worker_token", "fleet_token", "node_token", "authorization",
    "secret", "api_key", "access_key", "access_key_secret",
}
QUALIFICATION_KEYS = {
    "qualification", "wrapper", "wrapper_path", "run_root", "python", "pythonpath",
    "cuda_visible_devices", "owner", "vram_gb", "reserved_gb", "startup_timeout_s",
    "shutdown_timeout_s", "request_timeout_s", "gateway", "gateway_url", "facade", "facade_url",
}
IDENTITY_ENV_KEYS = (
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "HF_HUB_OFFLINE", "HARMONY_STREAMS",
    "PYTORCH_CUDA_ALLOC_CONF",
    "HARMONY_IMAGE_CONFIG", "HARMONY_IMAGE_GATEWAY_CONFIG", "CUDA_VISIBLE_DEVICES",
    "LIVESTACK_ACT_STORE", "LIVESTACK_BROKER_PORT", "LIVESTACK_BROKER_URL",
    "LIVESTACK_DEVICE_ID", "LIVESTACK_DEVICES", "LIVESTACK_DISPATCH",
    "LIVESTACK_FLEET_TOKENS_FILE", "LIVESTACK_HOST_ID", "LIVESTACK_LEDGER",
    "LIVESTACK_LEDGER_DIR", "LIVESTACK_LINK_PEERS", "LIVESTACK_MACHINE_ID",
    "LIVESTACK_MESH_ENABLED", "LIVESTACK_NODE_CONTROL_TOKEN_FILE",
    "LIVESTACK_NODE_HOST", "LIVESTACK_NODE_TOKENS_FILE", "LIVESTACK_PEERS",
    "LIVESTACK_REGISTER", "LIVESTACK_RELAYS",
)
PROC_ROOT = Path("/proc")


class QualificationError(RuntimeError):
    pass


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write_private(path, data):
    path = Path(path)
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _write_json(path, value):
    _write_private(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _write_handoff(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"JSON root must be an object: {path}")
    return value


def _option(config, name, default=None):
    nested = config.get("qualification") or {}
    if not isinstance(nested, dict):
        raise QualificationError("qualification must be an object")
    if name in nested:
        return nested[name]
    return config.get(name, default)


def _url_port(value):
    parsed = urllib.parse.urlparse(value)
    try:
        return parsed.port
    except ValueError as exc:
        raise QualificationError(f"invalid route URL: {value}") from exc


def _validate_route(value, name):
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{name} must be a nonempty URL")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise QualificationError(f"{name} must be an isolated loopback HTTP URL")
    if _url_port(value) in PRODUCTION_PORTS:
        raise QualificationError(f"{name} names a production route")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QualificationError(f"{name} must not contain credentials, query, or fragment")
    return value.rstrip("/")


def _validate_no_production_routes(config):
    def walk(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                lowered = str(child_key).lower()
                if lowered in SECRET_KEYS:
                    raise QualificationError(f"inline credential is not allowed: {child_key}")
                walk(child, lowered)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if value.startswith(("http://", "https://")):
                parsed = urllib.parse.urlparse(value)
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    raise QualificationError(f"{key or 'route'} is not an isolated loopback route")
                if _url_port(value) in PRODUCTION_PORTS:
                    raise QualificationError(f"{key or 'route'} names a production route")
            if "wrapper" in key and Path(value).resolve() == PRODUCTION_WRAPPER:
                raise QualificationError("the production wrapper must not be used")
        elif type(value) is int and key in {"port", "broker_port", "gateway_port"}:
            if value in PRODUCTION_PORTS:
                raise QualificationError(f"{key} names a production port")
    walk(config)


def _absolute_path(value, name):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise QualificationError(f"{name} must be an absolute path")
    return str(Path(value))


def _validate_owner(config):
    _validate_no_production_routes(config)
    model = config.get("model")
    revision = config.get("revision")
    alternative = model in (SDXL_MODEL, FLUX2_MODEL)
    if ((not alternative and (model != BASE_MODEL or revision != BASE_REVISION))
            or not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None):
        raise QualificationError("qualification requires a pinned base model and full revision")
    snapshot = _absolute_path(config.get("snapshot"), "base snapshot")
    if Path(snapshot).name != revision:
        raise QualificationError("base snapshot basename must equal the pinned base revision")
    quantization = config.get("quantization")
    if quantization not in UNIT_FOR_QUANTIZATION:
        raise QualificationError("qualification quantization must be nf4 or int8")
    has_depth = "depth_guide" in config
    alternative_units = {"int8": "flux_2_dev_depth_int8" if model == FLUX2_MODEL else "sdxl_depth_int8"}
    units = (alternative_units if alternative else
             UNIT_FOR_QUANTIZATION if has_depth else TEXT_UNIT_FOR_QUANTIZATION)
    unit = config.get("unit")
    if unit != units.get(quantization):
        raise QualificationError(f"crossed unit/quantization pair: {unit} with {quantization}")
    controlnet = None
    if has_depth:
        depth = config.get("depth_guide")
        if not isinstance(depth, dict):
            raise QualificationError("depth_guide configuration is required")
        if not alternative:
            expected_depth = {
                "model": CONTROLNET_MODEL,
                "revision": CONTROLNET_REVISION,
                "config_sha256": CONTROLNET_CONFIG_SHA256,
                "weight_sha256": CONTROLNET_WEIGHT_SHA256,
                "weight_bytes": CONTROLNET_WEIGHT_BYTES,
            }
            for key, expected in expected_depth.items():
                if depth.get(key) != expected:
                    raise QualificationError(f"depth_guide.{key} does not match the pinned identity")
        control_snapshot = _absolute_path(depth.get("snapshot"), "ControlNet snapshot")
        if Path(control_snapshot).name != depth.get("revision"):
            raise QualificationError("ControlNet snapshot basename must equal its pinned revision")
        controlnet_quantization = depth.get("quantization", "bf16")
        if controlnet_quantization not in ("bf16", "int8"):
            raise QualificationError("depth_guide.quantization must be bf16 or int8")
        controlnet = {key: depth[key] for key in (
            "model", "revision", "config_sha256", "weight_sha256", "weight_bytes")}
        controlnet["snapshot"] = control_snapshot
        controlnet["quantization"] = controlnet_quantization
    if alternative:
        if type(config.get("steps")) is not int or not 20 <= config["steps"] <= 50:
            raise QualificationError("alternative-model qualification requires 20 through 50 steps")
        cfg = config.get("guidance_scale", 7.5)
        if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or not 1 <= cfg <= 15:
            raise QualificationError("SDXL qualification requires a bounded guidance_scale")
    else:
        if config.get("steps") != 50 or type(config.get("steps")) is not int:
            raise QualificationError("qualification requires exactly 50 steps")
        cfg = config.get("true_cfg_scale")
        if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or cfg != 4:
            raise QualificationError("qualification requires true_cfg_scale 4")
        if config.get("negative_prompt") not in {"", QWEN_2512_NEGATIVE_PROMPT}:
            raise QualificationError("qualification requires an empty or pinned Qwen 2512 negative prompt")
        if config.get("max_sequence_length") != 512 or type(config.get("max_sequence_length")) is not int:
            raise QualificationError("qualification requires max_sequence_length 512")
    if config.get("text_encoder", "CPU FP32") != "CPU FP32":
        raise QualificationError("qualification requires CPU FP32 prompt encoding")
    host = config.get("host_id")
    device = config.get("device_id")
    if not isinstance(host, str) or not host or "OWNER" in host:
        raise QualificationError("host_id must name the owner host")
    if not isinstance(device, str) or not device or "OWNER" in device:
        raise QualificationError("device_id must name the owner-approved physical device")
    cuda = _option(config, "cuda_visible_devices")
    if isinstance(cuda, int) and not isinstance(cuda, bool):
        cuda = str(cuda)
    devices = config.get("device_ids", [device])
    if (not isinstance(devices, list) or not devices or devices[0] != device
            or any(not isinstance(value, str) or not value for value in devices)
            or len(set(devices)) != len(devices)):
        raise QualificationError("device_ids must be unique and start with device_id")
    cuda_devices = cuda.split(",") if isinstance(cuda, str) else []
    if (len(cuda_devices) != len(devices)
            or any(re.fullmatch(r"[0-9]+", value) is None for value in cuda_devices)):
        raise QualificationError("CUDA_VISIBLE_DEVICES must map every configured device in order")
    match = re.search(r"/gpu([0-9]+)$", device)
    if match and int(match.group(1)) != int(cuda_devices[0]):
        raise QualificationError("CUDA_VISIBLE_DEVICES and device_id select different physical devices")
    owner = _option(config, "owner", "acct_harmony-image-depth-qualification")
    if not isinstance(owner, str) or not owner.strip():
        raise QualificationError("gateway owner must be nonempty")
    wrapper = _option(config, "wrapper", _option(config, "wrapper_path"))
    wrapper = _absolute_path(wrapper, "owner wrapper")
    if Path(wrapper).resolve() == PRODUCTION_WRAPPER:
        raise QualificationError("the production wrapper must not be used")
    if not Path(wrapper).is_file():
        raise QualificationError(f"owner wrapper is not a file: {wrapper}")
    return {
        "host": host,
        "device_id": device,
        "device_ids": devices,
        "unit": unit,
        "quantization": quantization,
        "depth_guide": has_depth,
        "cuda_visible_devices": cuda,
        "owner": owner,
        "base": {"model": model, "revision": revision, "snapshot": snapshot},
        "controlnet": controlnet,
        "wrapper": wrapper,
    }


def _stable_owner(config):
    stable = {}
    for key, value in config.items():
        if key in QUALIFICATION_KEYS or key in {"broker", "port", "token_file", "fleet", "fleet_token_file"}:
            continue
        stable[key] = value
    return stable


def _allocate_ports(count=3):
    ports = []
    try:
        while len(ports) < count:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            if port not in PRODUCTION_PORTS and port not in ports:
                ports.append(port)
    except OSError as exc:
        raise QualificationError(f"cannot allocate isolated loopback ports: {exc}") from exc
    return ports


def _node_py_root():
    return Path(__file__).resolve().parents[2]


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise QualificationError(f"required package version is unavailable: {name}") from exc


def _git_revision(repo_root):
    git_path = Path(repo_root) / ".git"
    if git_path.is_file():
        line = git_path.read_text().strip()
        if not line.startswith("gitdir:"):
            raise QualificationError("cannot resolve Livestack gitdir")
        git_path = Path(line.split(":", 1)[1].strip())
        if not git_path.is_absolute():
            git_path = (Path(repo_root) / git_path).resolve()
    if not git_path.is_dir():
        raise QualificationError(f"Livestack Git metadata is unavailable: {repo_root}")
    head = (git_path / "HEAD").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{40}", head):
        return head
    if not head.startswith("ref: "):
        raise QualificationError("cannot resolve Livestack code revision")
    ref = head[5:].strip()
    candidates = [git_path]
    common = git_path / "commondir"
    if common.is_file():
        common_path = Path(common.read_text().strip())
        if not common_path.is_absolute():
            common_path = (git_path / common_path).resolve()
        candidates.append(common_path)
    for root in candidates:
        ref_path = root / ref
        if ref_path.is_file():
            revision = ref_path.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{40}", revision):
                return revision
        packed = root / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    revision = line.split(" ", 1)[0]
                    if re.fullmatch(r"[0-9a-f]{40}", revision):
                        return revision
    raise QualificationError(f"cannot resolve Livestack code revision for {ref}")


def _identity_environment(env):
    return {key: env[key] for key in IDENTITY_ENV_KEYS if key in env}


def _proc_dir(pid):
    return PROC_ROOT / str(pid)


def _start_ticks(pid):
    try:
        raw = (_proc_dir(pid) / "stat").read_text()
        remainder = raw[raw.rfind(")") + 2:].split()
        return int(remainder[19])
    except (OSError, IndexError, ValueError) as exc:
        raise QualificationError(f"cannot read start ticks for PID {pid}") from exc


def _proc_identity(pid):
    directory = _proc_dir(pid)
    if not directory.exists():
        return None
    try:
        argv = [os.fsdecode(part) for part in (directory / "cmdline").read_bytes().split(b"\0") if part]
        executable = os.path.realpath(os.readlink(directory / "exe"))
        environ = {}
        for entry in (directory / "environ").read_bytes().split(b"\0"):
            if b"=" not in entry:
                continue
            key, value = entry.split(b"=", 1)
            key = os.fsdecode(key)
            if key in IDENTITY_ENV_KEYS:
                environ[key] = os.fsdecode(value)
        return {
            "pid": pid,
            "start_ticks": _start_ticks(pid),
            "argv": argv,
            "executable": executable,
            "environment": environ,
        }
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QualificationError(f"cannot inspect PID {pid}: {exc}") from exc


def _record_process(role, process, argv, log_path, env):
    pid = process.pid
    try:
        pgid = os.getpgid(pid)
    except OSError as exc:
        raise QualificationError(f"cannot read PGID for {role} PID {pid}: {exc}") from exc
    deadline = time.monotonic() + 5
    while True:
        identity = _proc_identity(pid)
        if identity is not None and identity["argv"]:
            break
        if process.poll() is not None:
            raise QualificationError(f"{role} exited before process identity could be recorded; see {log_path}")
        if time.monotonic() >= deadline:
            raise QualificationError(f"{role} process identity unavailable after 5 seconds")
        time.sleep(0.01)
    if identity["argv"] != list(argv):
        raise QualificationError(f"{role} argv changed during launch: expected {list(argv)!r}, got {identity['argv']!r}")
    if pgid != pid:
        raise QualificationError(f"{role} was not started in its own session")
    return {
        **identity,
        "pgid": pgid,
        "log": str(log_path),
    }


def _verify_process(record, role, require_alive=True):
    pid = record.get("pid")
    if type(pid) is not int or pid <= 1:
        raise QualificationError(f"invalid recorded {role} PID")
    identity = _proc_identity(pid)
    if identity is None:
        if require_alive:
            raise QualificationError(f"recorded {role} PID {pid} is not running")
        return False
    for key in ("start_ticks", "argv", "executable"):
        if identity[key] != record.get(key):
            raise QualificationError(f"recorded {role} {key} does not match PID {pid}")
    if identity["environment"] != record.get("environment"):
        raise QualificationError(f"recorded {role} environment does not match PID {pid}")
    try:
        pgid = os.getpgid(pid)
    except OSError as exc:
        raise QualificationError(f"cannot revalidate {role} PGID: {exc}") from exc
    if pgid != record.get("pgid") or pgid != pid:
        raise QualificationError(f"recorded {role} PGID does not match PID {pid}")
    return True


def _spawn(role, python, module, env, log_path, popen=None):
    argv = [python, "-m", module]
    log_path = Path(log_path)
    with log_path.open("ab") as log:
        process = (popen or subprocess.Popen)(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(_node_py_root()),
            close_fds=True,
            start_new_session=True,
        )
    try:
        return process, _record_process(role, process, argv, log_path, env)
    except Exception:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            pass
        raise


def _progress(stage, dispatch=None):
    print(json.dumps({
        "event": "depth_lifecycle",
        "stage": stage,
        "run_dir": dispatch.get("run_dir") if isinstance(dispatch, dict) else None,
    }, sort_keys=True), flush=True)


def _http_json(base, method, path, body=None, token=None, timeout=30):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = _canonical(body)
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        if token:
            detail = detail.replace(token, "<redacted>")
        raise QualificationError(f"HTTP {exc.code} {method} {base}{path}: {detail[:500]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise QualificationError(f"HTTP {method} {base}{path} failed: {exc}") from exc
    if status < 200 or status >= 300:
        raise QualificationError(f"HTTP {status} {method} {base}{path}")
    try:
        value = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise QualificationError(f"HTTP {method} {base}{path} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"HTTP {method} {base}{path} returned a non-object")
    return value


def _wait_json(description, base, path, ready, processes, handles, timeout_s):
    _progress("wait_start:" + description, processes)
    deadline = time.monotonic() + timeout_s
    last_error = None
    while True:
        for pid, process in handles.items():
            if process.poll() is not None:
                record = next(row for row in processes.values() if row["pid"] == pid)
                raise QualificationError(
                    f"recorded PID {pid} exited while waiting for {description}; see {record['log']}")
        try:
            value = _http_json(base, "GET", path, timeout=5)
        except QualificationError as exc:
            last_error = exc
        else:
            if ready(value):
                _progress("wait_ok:" + description, processes)
                return value
        if time.monotonic() >= deadline:
            detail = f": {last_error}" if last_error else ""
            raise QualificationError(f"timed out waiting for {description}{detail}")
        time.sleep(0.2)


def _validate_capability(capability, dispatch):
    expected = dispatch["expected"]
    if capability.get("kind") != "imagegen":
        raise QualificationError("worker facade has the wrong capability kind")
    if capability.get("host_id") != expected["host"]:
        raise QualificationError("worker facade has the wrong host")
    if capability.get("device_id") != expected["device_id"]:
        raise QualificationError("worker facade has the wrong device")
    if capability.get("units") != [expected["unit"]]:
        raise QualificationError("worker facade does not expose exactly the expected unit")


def _validate_residence(residence, dispatch):
    expected = dispatch["expected"]
    if residence.get("host_id") != expected["host"] or residence.get("device_id") != expected["device_id"]:
        raise QualificationError("worker residence has the wrong host or device")
    units = residence.get("units")
    if not isinstance(units, list) or len(units) != 1 or units[0].get("kind") != expected["unit"]:
        raise QualificationError("worker residence does not expose exactly the expected unit")


def _model_peer(peers, dispatch):
    rows = peers.get("peers")
    if not isinstance(rows, list):
        raise QualificationError("isolated broker has unexpected peers")
    meters = {"local-meter://" + device for device in dispatch["expected"]["device_ids"][1:]}
    model_rows = []
    meter_rows = set()
    for row in rows:
        peer = row.get("peer")
        if peer in meters:
            meter_rows.add(peer)
        else:
            model_rows.append(row)
    if meter_rows != meters or len(model_rows) != 1:
        raise QualificationError("isolated broker must have one worker and every configured device meter")
    row = model_rows[0]
    if row.get("peer") != dispatch["facade_url"]:
        raise QualificationError("isolated broker has an unexpected peer URL")
    if row.get("host_id") not in (None, dispatch["expected"]["host"]):
        raise QualificationError("isolated peer has the wrong owner host")
    return row


def _validate_peers(peers, dispatch):
    if peers.get("host_id") != dispatch["broker_host_id"]:
        raise QualificationError("broker /peers has the wrong unique host id")
    if _model_peer(peers, dispatch).get("state") != "fresh":
        raise QualificationError("isolated peer is not fresh")


def _peer_ready(peers, dispatch):
    row = _model_peer(peers, dispatch)
    return peers.get("host_id") == dispatch["broker_host_id"] and row.get("state") == "fresh"


def _fleet_nodes(view):
    hosts = view.get("hosts")
    if not isinstance(hosts, dict):
        raise QualificationError("fleet hosts must be an object")
    nodes = []
    for host, row in hosts.items():
        if not isinstance(row, dict) or not isinstance(row.get("nodes"), list):
            raise QualificationError("fleet host has no node list")
        for node in row["nodes"]:
            if not str(node.get("peer", "")).startswith("local-meter://"):
                nodes.append((host, node))
    return nodes


def _validate_fleet(view, dispatch):
    expected = dispatch["expected"]
    if view.get("dispatch") is not True:
        raise QualificationError("qualification broker is not in apply dispatch mode")
    if view.get("peers") != len(expected["device_ids"]):
        raise QualificationError("qualification fleet must contain the worker and every device meter")
    nodes = _fleet_nodes(view)
    if len(nodes) != 1:
        kinds = sorted({unit.get("kind") for _, node in nodes for unit in node.get("units", []) if isinstance(unit, dict)})
        suffix = f"; mixed-kind collision: {', '.join(kinds)}" if len(kinds) > 1 else ""
        raise QualificationError("qualification fleet has a peer or variant collision" + suffix)
    host, node = nodes[0]
    if host != expected["host"] or node.get("peer") != dispatch["facade_url"]:
        raise QualificationError("qualification fleet has an unexpected host or facade")
    if node.get("state") != "fresh":
        raise QualificationError("qualification fleet peer is not fresh")
    if node.get("device_id") != expected["device_id"]:
        raise QualificationError("qualification fleet selected the wrong device")
    units = node.get("units")
    if not isinstance(units, list) or len(units) != 1:
        raise QualificationError("qualification fleet has a mixed-kind or unit collision")
    unit = units[0]
    if unit.get("kind") != expected["unit"]:
        raise QualificationError("qualification fleet has the wrong expected unit")
    attributes = unit.get("attributes")
    if not isinstance(attributes, dict):
        raise QualificationError("qualification unit has no attributes")
    base = dispatch["base"]
    required = {
        "class": "imagegen",
        "task": "text_to_image",
        "model": base["model"],
        "revision": base["revision"],
        "quantization": expected["quantization"],
        **_generation_settings(dispatch["owner_variant"]),
    }
    if expected["depth_guide"]:
        control = dispatch["controlnet"]
        required.update({
            "depth_guide": True,
            "controlnet_model": control["model"],
            "controlnet_revision": control["revision"],
            "controlnet_config_sha256": control["config_sha256"],
            "controlnet_quantization": control["quantization"],
        })
    else:
        forbidden = {"depth_guide", "controlnet"}
        if any(key in forbidden or key.startswith("controlnet_") for key in attributes):
            raise QualificationError("text-only qualification unit must not advertise depth or ControlNet attributes")
    for key, expected_value in required.items():
        if attributes.get(key) != expected_value:
            raise QualificationError(f"qualification unit attribute {key} does not match")
    if "snapshot" in attributes and attributes["snapshot"] != base["snapshot"]:
        raise QualificationError("qualification unit has the wrong base snapshot")
    if expected["depth_guide"] and attributes.get("depth_guide") is not True:
        raise QualificationError("qualification unit does not advertise literal depth guidance")
    alternative_units = {"int8": "flux_2_dev_depth_int8" if base["model"] == FLUX2_MODEL else "sdxl_depth_int8"}
    unit_map = (alternative_units if base["model"] in (SDXL_MODEL, FLUX2_MODEL) else
                UNIT_FOR_QUANTIZATION if expected["depth_guide"] else TEXT_UNIT_FOR_QUANTIZATION)
    if unit_map.get(attributes.get("quantization")) != unit.get("kind"):
        raise QualificationError("qualification fleet has a crossed unit/quantization pair")


def _fingerprint_material(dispatch):
    material = {
        "owner_host": dispatch["owner_host"],
        "expected": {key: dispatch["expected"][key] for key in (
            "host", "device_id", "device_ids", "unit", "quantization", "depth_guide", "cuda_visible_devices", "logical_device")},
        "base": dispatch["base"],
        "controlnet": dispatch["controlnet"],
        "generation_settings": dispatch["generation_settings"],
        "versions": dispatch["versions"],
        "python": dispatch["python"],
        "pythonpath": dispatch["pythonpath"],
        "wrapper_sha256": dispatch["wrapper"]["sha256"],
        "owner_variant_sha256": dispatch["owner_variant_sha256"],
        "livestack_code_revision": dispatch["livestack_code_revision"],
    }
    if dispatch.get("source_manifest_sha256") is not None:
        material["source_manifest_sha256"] = dispatch["source_manifest_sha256"]
    return material


def _environment_fingerprint(dispatch):
    return _sha256_bytes(_canonical(_fingerprint_material(dispatch)))


def _path_inside(path, root, name):
    try:
        resolved = Path(path).resolve(strict=True)
        resolved.relative_to(Path(root).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QualificationError(f"{name} must name an existing file inside the run dir") from exc
    if not resolved.is_file():
        raise QualificationError(f"{name} is not a file")
    return resolved


def _verify_assets(dispatch):
    run_dir = Path(dispatch["run_dir"])
    mode = run_dir.stat().st_mode & 0o777
    if mode != 0o700:
        raise QualificationError("qualification run dir must be mode 0700")
    for entry in dispatch["configs"].values():
        path = _path_inside(entry["path"], run_dir, "configuration")
        if _sha256_file(path) != entry["sha256"]:
            raise QualificationError(f"configuration hash mismatch: {path}")
    wrapper = dispatch["wrapper"]
    path = _path_inside(wrapper["path"], run_dir, "staged wrapper")
    if _sha256_file(path) != wrapper["sha256"]:
        raise QualificationError("staged wrapper hash mismatch")


def _validate_dispatch(dispatch, verify_fingerprint=True):
    if not isinstance(dispatch, dict) or dispatch.get("schema") != SCHEMA:
        raise QualificationError("dispatch handoff has the wrong schema")
    if not isinstance(dispatch.get("run_id"), str) or RUN_ID_RE.fullmatch(dispatch["run_id"]) is None:
        raise QualificationError("dispatch handoff has an invalid run_id")
    run_dir = Path(dispatch.get("run_dir", ""))
    if not run_dir.is_absolute():
        raise QualificationError("dispatch run_dir must be absolute")
    for key in ("broker_url", "gateway_url"):
        _validate_route(dispatch.get(key), key)
    facade = _validate_route(dispatch.get("facade_url"), "facade_url")
    if not facade.endswith("/livestack"):
        raise QualificationError("facade_url must end in /livestack")
    expected = dispatch.get("expected")
    if not isinstance(expected, dict):
        raise QualificationError("dispatch expected identity is missing")
    depth_guide = expected.get("depth_guide")
    if type(depth_guide) is not bool:
        raise QualificationError("dispatch expected depth_guide must be a boolean")
    base_model = dispatch.get("base", {}).get("model")
    alternative_units = {"int8": "flux_2_dev_depth_int8" if base_model == FLUX2_MODEL else "sdxl_depth_int8"}
    units = (alternative_units if base_model in (SDXL_MODEL, FLUX2_MODEL) else
             UNIT_FOR_QUANTIZATION if depth_guide else TEXT_UNIT_FOR_QUANTIZATION)
    if expected.get("unit") != units.get(expected.get("quantization")):
        raise QualificationError("dispatch contains a crossed unit/quantization pair")
    if not depth_guide and dispatch.get("controlnet") is not None:
        raise QualificationError("text-only dispatch must not contain ControlNet identity")
    if depth_guide:
        controlnet = dispatch.get("controlnet")
        if (not isinstance(controlnet, dict)
                or controlnet.get("quantization") not in ("bf16", "int8")):
            raise QualificationError("dispatch ControlNet quantization must be bf16 or int8")
    if expected.get("host") != dispatch.get("owner_host"):
        raise QualificationError("dispatch owner host and expected host differ")
    if dispatch.get("generation_settings") != _generation_settings(dispatch["owner_variant"]):
        raise QualificationError("dispatch generation settings do not match qualification")
    if not isinstance(dispatch.get("processes"), dict) or set(dispatch["processes"]) != set(MODULES):
        raise QualificationError("dispatch must record hostd, worker, and gateway")
    for role, module in MODULES.items():
        record = dispatch["processes"][role]
        if record.get("pgid") != record.get("pid"):
            raise QualificationError(f"dispatch {role} PGID must equal its isolated PID")
        argv = record.get("argv")
        if not isinstance(argv, list) or len(argv) != 3 or argv[1:3] != ["-m", module]:
            raise QualificationError(f"dispatch {role} argv is not the expected module")
        _path_inside(record.get("log", ""), run_dir, f"{role} log")
    if not isinstance(dispatch.get("configs"), dict) or set(dispatch["configs"]) != {"worker", "gateway", "hostd_launch"}:
        raise QualificationError("dispatch config identities are incomplete")
    for entry in dispatch["configs"].values():
        if SHA256_RE.fullmatch(str(entry.get("sha256", ""))) is None:
            raise QualificationError("dispatch contains an invalid config hash")
    if SHA256_RE.fullmatch(str(dispatch.get("wrapper", {}).get("sha256", ""))) is None:
        raise QualificationError("dispatch contains an invalid wrapper hash")
    stable_hash = _sha256_bytes(_canonical(dispatch.get("owner_variant")))
    if stable_hash != dispatch.get("owner_variant_sha256"):
        raise QualificationError("dispatch owner variant hash does not match")
    if dispatch.get("source_manifest_sha256") is not None and SHA256_RE.fullmatch(dispatch["source_manifest_sha256"]) is None:
        raise QualificationError("dispatch contains an invalid source manifest hash")
    if verify_fingerprint and _environment_fingerprint(dispatch) != dispatch.get("environment_fingerprint"):
        raise QualificationError("dispatch environment fingerprint does not match")


def _load_dispatch(path):
    dispatch = _read_json(path)
    _validate_dispatch(dispatch)
    return dispatch


def _read_token(path, run_dir):
    path = _path_inside(path, run_dir, "token file")
    if path.stat().st_mode & 0o777 != 0o600:
        raise QualificationError("token file must be mode 0600")
    token = path.read_text().strip()
    if len(token) < 32 or "\n" in token or "\r" in token:
        raise QualificationError("token file must contain one fresh raw token")
    return token


def _assert_no_credentials(dispatch, tokens):
    serialized = json.dumps(dispatch, sort_keys=True)
    for token in tokens:
        if token and token in serialized:
            raise QualificationError("raw credential would enter the dispatch handoff")
    for record in dispatch.get("processes", {}).values():
        argv_text = "\0".join(record.get("argv", []))
        for token in tokens:
            if token and token in argv_text:
                raise QualificationError("raw credential would enter process argv")


def _prepare_up(config_path, output_path):
    source = _read_json(config_path)
    owner = _validate_owner(source)
    stable = _stable_owner(source)
    output_path = Path(output_path).resolve()
    run_root = Path(_option(source, "run_root", str(output_path.parent / "depth-qualification-runs"))).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    run_id = secrets.token_hex(8)
    run_dir = Path(tempfile.mkdtemp(prefix=f"depth-{run_id}-", dir=str(run_root)))
    os.chmod(run_dir, 0o700)
    for name in ("logs", "state", "ledger", "home"):
        (run_dir / name).mkdir(mode=0o700)
    python = os.path.abspath(_option(source, "python", sys.executable))
    if not Path(python).is_file():
        raise QualificationError("configured Python executable does not exist")
    configured_pythonpath = _option(source, "pythonpath", [])
    if isinstance(configured_pythonpath, str):
        configured_pythonpath = [part for part in configured_pythonpath.split(os.pathsep) if part]
    if not isinstance(configured_pythonpath, list) or not all(isinstance(part, str) for part in configured_pythonpath):
        raise QualificationError("pythonpath must be a string or list of strings")
    pythonpath = [str(_node_py_root())]
    for part in configured_pythonpath:
        resolved = str(Path(part).resolve())
        if resolved not in pythonpath:
            pythonpath.append(resolved)
    pythonpath_text = os.pathsep.join(pythonpath)
    versions = {"torch": _package_version("torch"), "diffusers": _package_version("diffusers")}
    revision_override = _option(source, "code_revision", _option(source, "livestack_code_revision"))
    if revision_override is not None and re.fullmatch(r"[0-9a-f]{40}", revision_override) is None:
        raise QualificationError("code_revision must be a full lowercase Git revision")
    code_revision = revision_override or _git_revision(_node_py_root().parent)
    source_manifest_sha256 = _option(source, "source_manifest_sha256")
    if source_manifest_sha256 is not None and SHA256_RE.fullmatch(source_manifest_sha256) is None:
        raise QualificationError("source_manifest_sha256 must be 64 lowercase hexadecimal characters")
    broker_port, worker_port, gateway_port = _allocate_ports(3)
    broker_url = f"http://127.0.0.1:{broker_port}"
    facade_url = f"http://127.0.0.1:{worker_port}/livestack"
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    for name, url in (("broker", broker_url), ("facade", facade_url), ("gateway", gateway_url)):
        _validate_route(url, name)
    worker_token = secrets.token_urlsafe(48)
    fleet_token = secrets.token_urlsafe(48)
    paths = {
        "worker_token": run_dir / "worker.token",
        "fleet_token": run_dir / "fleet.token",
        "node_principals": run_dir / "node-principals.json",
        "fleet_principals": run_dir / "fleet-principals.json",
        "worker_config": run_dir / "worker.json",
        "gateway_config": run_dir / "gateway.json",
        "hostd_config": run_dir / "hostd-launch.json",
    }
    _write_private(paths["worker_token"], worker_token + "\n")
    _write_private(paths["fleet_token"], fleet_token + "\n")
    _write_json(paths["node_principals"], {
        worker_token: {"name": "depth-qualification-hostd", "owner": owner["owner"]},
    })
    admission_owner = "harmony-image" if source.get("exclusive_device") is not None else owner["owner"]
    _write_json(paths["fleet_principals"], {
        fleet_token: {"name": "depth-qualification-gateway", "owner": admission_owner},
        worker_token: {"name": "depth-qualification-worker", "owner": "harmony-image"},
    })
    staged_wrapper = run_dir / f"owner-wrapper-{run_id}.py"
    with Path(owner["wrapper"]).open("rb") as source_handle, staged_wrapper.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle)
    os.chmod(staged_wrapper, 0o700)
    wrapper_hash = _sha256_file(staged_wrapper)
    worker_config = dict(source)
    for key in QUALIFICATION_KEYS | {"fleet", "fleet_token_file"}:
        worker_config.pop(key, None)
    worker_config.update({
        "broker": broker_url,
        "port": worker_port,
        "token_file": str(paths["worker_token"]),
        "fleet_token_file": str(paths["worker_token"]),
    })
    gateway_config = {
        "worker_token_file": str(paths["worker_token"]),
        "fleet_token_file": str(paths["fleet_token"]),
        "fleet": broker_url,
        "owner": admission_owner,
        "port": gateway_port,
    }
    if source.get("request_timeout_s") is not None:
        gateway_config["request_timeout_s"] = source["request_timeout_s"]
    if source.get("exclusive_device") is not None:
        gateway_config.update(device_id=source["device_id"], device_ids=owner["device_ids"],
                              exclusive_device=source["exclusive_device"])
    common_env = {
        "PATH": os.defpath,
        "HOME": str(run_dir / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": pythonpath_text,
        "HF_HUB_OFFLINE": "1",
        "HARMONY_STREAMS": "0",
        "LIVESTACK_MESH_ENABLED": "0",
        "LIVESTACK_LEDGER": "0",
        "LIVESTACK_LEDGER_DIR": str(run_dir / "ledger"),
        "LIVESTACK_LINK_PEERS": "",
        "LIVESTACK_RELAYS": "",
        "LIVESTACK_FLEET_POOLS": "",
    }
    broker_host_id = f"depth-qualification-{run_id}"
    hostd_env = {
        **common_env,
        "LIVESTACK_DISPATCH": "apply",
        "LIVESTACK_BROKER_PORT": str(broker_port),
        "LIVESTACK_HOST_ID": broker_host_id,
        "LIVESTACK_PEERS": facade_url,
        "LIVESTACK_NODE_CONTROL_TOKEN_FILE": str(paths["worker_token"]),
        "LIVESTACK_FLEET_TOKENS_FILE": str(paths["fleet_principals"]),
        "LIVESTACK_DEVICE_METERS": json.dumps({
            device_id: int(owner["cuda_visible_devices"].split(",")[index])
            for index, device_id in enumerate(owner["device_ids"][1:], 1)
        }, separators=(",", ":")),
        "LIVESTACK_DEVICES": json.dumps({
            device_id: {
                "vram_gb": _option(source, "vram_gb", 24),
                "reserved_gb": _option(source, "reserved_gb", 2),
            } for device_id in owner["device_ids"]
        }, separators=(",", ":")),
    }
    worker_env = {
        **common_env,
        "HARMONY_IMAGE_CONFIG": str(paths["worker_config"]),
        "LIVESTACK_REGISTER": "0",
        "LIVESTACK_BROKER_URL": broker_url,
        "LIVESTACK_NODE_HOST": "127.0.0.1",
        "LIVESTACK_NODE_TOKENS_FILE": str(paths["node_principals"]),
        "LIVESTACK_DEVICE_ID": owner["device_id"],
        "LIVESTACK_MACHINE_ID": owner["host"],
        "LIVESTACK_ACT_STORE": str(run_dir / "state" / "activation-imagegen.json"),
        "CUDA_VISIBLE_DEVICES": owner["cuda_visible_devices"],
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    }
    gateway_env = {
        **common_env,
        "HARMONY_IMAGE_GATEWAY_CONFIG": str(paths["gateway_config"]),
        "LIVESTACK_REGISTER": "0",
        "LIVESTACK_BROKER_URL": broker_url,
    }
    hostd_launch = {
        "module": MODULES["hostd"],
        "broker_url": broker_url,
        "facade_url": facade_url,
        "state_dir": str(run_dir / "state"),
        "environment": hostd_env,
    }
    _write_json(paths["worker_config"], worker_config)
    _write_json(paths["gateway_config"], gateway_config)
    _write_json(paths["hostd_config"], hostd_launch)
    dispatch = {
        "schema": SCHEMA,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "broker_url": broker_url,
        "gateway_url": gateway_url,
        "facade_url": facade_url,
        "broker_host_id": broker_host_id,
        "owner_host": owner["host"],
        "expected": {
            "host": owner["host"],
            "device_id": owner["device_id"],
            "device_ids": owner["device_ids"],
            "unit": owner["unit"],
            "quantization": owner["quantization"],
            "depth_guide": owner["depth_guide"],
            "cuda_visible_devices": owner["cuda_visible_devices"],
            "logical_device": "cuda:0",
        },
        "base": owner["base"],
        "controlnet": owner["controlnet"],
        "generation_settings": _generation_settings(stable),
        "versions": versions,
        "python": python,
        "pythonpath": pythonpath_text,
        "token_files": {
            "worker": str(paths["worker_token"]),
            "fleet": str(paths["fleet_token"]),
            "node_control": str(paths["worker_token"]),
            "node_principals": str(paths["node_principals"]),
            "fleet_principals": str(paths["fleet_principals"]),
        },
        "wrapper": {"path": str(staged_wrapper), "sha256": wrapper_hash},
        "configs": {
            "worker": {"path": str(paths["worker_config"]), "sha256": _sha256_file(paths["worker_config"])},
            "gateway": {"path": str(paths["gateway_config"]), "sha256": _sha256_file(paths["gateway_config"])},
            "hostd_launch": {"path": str(paths["hostd_config"]), "sha256": _sha256_file(paths["hostd_config"])},
        },
        "owner_variant": stable,
        "owner_variant_sha256": _sha256_bytes(_canonical(stable)),
        "livestack_code_revision": code_revision,
        "processes": {},
    }
    if source_manifest_sha256 is not None:
        dispatch["source_manifest_sha256"] = source_manifest_sha256
    dispatch["environment_fingerprint"] = _environment_fingerprint(dispatch)
    _assert_no_credentials(dispatch, (worker_token, fleet_token))
    return dispatch, {"hostd": hostd_env, "worker": worker_env, "gateway": gateway_env}, (worker_token, fleet_token)


def _process_running(record, handles):
    process = handles.get(record.get("pid")) if handles else None
    if process is not None and process.poll() is not None:
        return False
    return _proc_identity(record["pid"]) is not None


def _wait_exit(record, handles, deadline):
    while _process_running(record, handles):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _stop_processes(dispatch, handles=None, shutdown_timeout_s=10):
    errors = []
    for role in STOP_ORDER:
        record = dispatch["processes"].get(role)
        if record is None:
            continue
        try:
            if not _verify_process(record, role, require_alive=False):
                continue
            os.killpg(record["pgid"], signal.SIGTERM)
            if _wait_exit(record, handles or {}, time.monotonic() + shutdown_timeout_s):
                continue
            _verify_assets(dispatch)
            if not _verify_process(record, role, require_alive=False):
                continue
            os.killpg(record["pgid"], signal.SIGKILL)
            if not _wait_exit(record, handles or {}, time.monotonic() + shutdown_timeout_s):
                raise QualificationError(f"{role} did not exit after SIGKILL")
        except (OSError, QualificationError) as exc:
            errors.append(f"{role}: {exc}")
    if errors:
        raise QualificationError("shutdown refused or incomplete: " + "; ".join(errors))


def up(config_path, output_path):
    dispatch = None
    environments = None
    handles = {}
    source = _read_json(config_path)
    timeout_s = float(_option(source, "startup_timeout_s", 90))
    shutdown_timeout_s = float(_option(source, "shutdown_timeout_s", 10))
    if timeout_s <= 0 or shutdown_timeout_s <= 0:
        raise QualificationError("startup and shutdown timeouts must be positive")
    try:
        dispatch, environments, tokens = _prepare_up(config_path, output_path)
        run_dir = Path(dispatch["run_dir"])
        _progress("prepared", dispatch)
        for role in ("hostd", "worker"):
            _progress("spawn:" + role, dispatch)
            process, record = _spawn(
                role, dispatch["python"], MODULES[role], environments[role], run_dir / "logs" / f"{role}.log")
            handles[process.pid] = process
            dispatch["processes"][role] = record
            if role == "hostd":
                _wait_json(
                    "isolated broker /peers", dispatch["broker_url"], "/peers",
                    lambda value: value.get("host_id") == dispatch["broker_host_id"],
                    dispatch["processes"], handles, timeout_s)
                continue
            _wait_json(
                "worker facade health", dispatch["facade_url"], "/health",
                lambda value: value.get("ok") is True or value.get("status") == "ok",
                dispatch["processes"], handles, timeout_s)
            capability = _wait_json(
                "worker facade capability", dispatch["facade_url"], "/capability",
                lambda value: bool(value.get("units")), dispatch["processes"], handles, timeout_s)
            _validate_capability(capability, dispatch)
            peers = _wait_json(
                "fresh isolated worker peer", dispatch["broker_url"], "/peers",
                lambda value: _peer_ready(value, dispatch), dispatch["processes"], handles, timeout_s)
            _validate_peers(peers, dispatch)
            _progress("before_residence", dispatch)
            residence = _http_json(dispatch["facade_url"], "GET", "/residence", timeout=10)
            _validate_residence(residence, dispatch)
            _progress("before_plan", dispatch)
            _http_json(dispatch["broker_url"], "GET", "/plan", timeout=30)
            _progress("before_fleet", dispatch)
            fleet = _http_json(dispatch["broker_url"], "GET", "/fleet", timeout=30)
            _validate_fleet(fleet, dispatch)
        _progress("spawn:gateway", dispatch)
        process, record = _spawn(
            "gateway", dispatch["python"], MODULES["gateway"], environments["gateway"],
            run_dir / "logs" / "gateway.log")
        handles[process.pid] = process
        dispatch["processes"]["gateway"] = record
        health = _wait_json(
            "gateway fleet health", dispatch["gateway_url"], "/health",
            lambda value: value.get("ok") is True and value.get("fleet") == dispatch["broker_url"],
            dispatch["processes"], handles, timeout_s)
        if health.get("fleet") != dispatch["broker_url"]:
            raise QualificationError("gateway health names the wrong fleet")
        _assert_no_credentials(dispatch, tokens)
        _validate_dispatch(dispatch)
        _verify_assets(dispatch)
        for role, record in dispatch["processes"].items():
            _verify_process(record, role)
        _write_handoff(output_path, dispatch)
        return dispatch
    except Exception as exc:
        if dispatch is not None and dispatch.get("processes"):
            try:
                _stop_processes(dispatch, handles, shutdown_timeout_s)
            except Exception as cleanup:
                raise QualificationError(f"{exc}; startup cleanup failed: {cleanup}") from exc
        raise


def _validate_smoke_request(request, dispatch):
    if dispatch["expected"].get("depth_guide") is not True:
        raise QualificationError("guided smoke validation requires a depth-guide dispatch")
    requirements = request.get("harmony_requires")
    if not isinstance(requirements, dict):
        raise QualificationError("smoke request must name explicit harmony_requires")
    expected = dispatch["expected"]
    base = dispatch["base"]
    control = dispatch["controlnet"]
    required = {
        "class": "imagegen",
        "task": "text_to_image",
        "depth_guide": True,
        "model": base["model"],
        "revision": base["revision"],
        "controlnet_model": control["model"],
        "controlnet_revision": control["revision"],
        "controlnet_config_sha256": control["config_sha256"],
        "quantization": expected["quantization"],
    }
    for key, value in required.items():
        if requirements.get(key) != value:
            raise QualificationError(f"smoke request requirement {key} does not match the dispatch")
    if requirements.get("depth_guide") is not True:
        raise QualificationError("smoke request depth_guide must be literal true")
    if not isinstance(request.get("prompt"), str) or not request["prompt"].strip():
        raise QualificationError("smoke request must contain an explicit prompt")
    width, height = request.get("width"), request.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise QualificationError("smoke request dimensions must be positive integers")
    if type(request.get("seed")) is not int:
        raise QualificationError("smoke request must contain an explicit integer seed")
    scale = request.get("controlnet_conditioning_scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or not 0.8 <= scale <= 1.0:
        raise QualificationError("smoke request has an invalid conditioning scale")
    guide = request.get("control_image")
    if not isinstance(guide, dict) or guide.get("media_type") != "image/png":
        raise QualificationError("smoke request must contain an explicit PNG control image")
    if SHA256_RE.fullmatch(str(guide.get("sha256", ""))) is None:
        raise QualificationError("smoke request guide hash must be lowercase SHA-256")
    try:
        guide_bytes = base64.b64decode(guide.get("b64_json", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise QualificationError("smoke request guide is not strict base64") from exc
    if len(guide_bytes) > 8 * 1024 * 1024 or _sha256_bytes(guide_bytes) != guide["sha256"]:
        raise QualificationError("smoke request guide size or hash does not match")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(guide_bytes)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise QualificationError("smoke request guide format or dimensions do not match")
            if getattr(image, "n_frames", 1) != 1:
                raise QualificationError("smoke request guide must contain one image")
            image.convert("RGB").load()
    except QualificationError:
        raise
    except Exception as exc:
        raise QualificationError("smoke request guide is not a readable PNG") from exc


def _actual_png(response, request):
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise QualificationError("smoke response must contain exactly one image")
    try:
        png = base64.b64decode(data[0].get("b64_json", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise QualificationError("smoke response image is not strict base64") from exc
    from PIL import Image, ImageStat
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise QualificationError("smoke response is not an actual PNG")
            if getattr(image, "n_frames", 1) != 1:
                raise QualificationError("smoke response PNG must contain one image")
            image.load()
            size = image.size
            rgb = image.convert("RGB")
            nonblank = max(ImageStat.Stat(rgb).stddev) >= 2
    except QualificationError:
        raise
    except Exception as exc:
        raise QualificationError("smoke response PNG cannot be decoded") from exc
    if size != (request["width"], request["height"]):
        raise QualificationError("actual PNG dimensions do not match the explicit request")
    if not nonblank:
        raise QualificationError("actual PNG is blank")
    return png, size


def _verify_grant(grant, name, dispatch):
    if not isinstance(grant, dict) or grant.get("granted") is not True:
        raise QualificationError(f"{name} was not granted")
    if grant.get("degraded"):
        raise QualificationError(f"{name} is degraded")
    expected = dispatch["expected"]
    if grant.get("kind") != expected["unit"] or grant.get("device_id") != expected["device_id"]:
        raise QualificationError(f"{name} has a mismatched unit or device")


def _verify_smoke_response(response, request, dispatch):
    png, size = _actual_png(response, request)
    harmony = response.get("harmony")
    if not isinstance(harmony, dict):
        raise QualificationError("smoke response has no harmony evidence")
    expected = dispatch["expected"]
    base = dispatch["base"]
    required = {
        "model": base["model"],
        "revision": base["revision"],
        "unit": expected["unit"],
        "host": expected["host"],
        "device_id": expected["device_id"],
        "device_ids": expected["device_ids"],
        "prompt": request["prompt"],
        "seed": request["seed"],
        "requirements": request["harmony_requires"],
        "quantization": expected["quantization"],
        "steps": dispatch["generation_settings"]["steps"],
        "worker_endpoint": dispatch["facade_url"][:-len("/livestack")],
    }
    for key, value in required.items():
        if harmony.get(key) != value:
            raise QualificationError(f"smoke harmony evidence {key} does not match")
    if harmony.get("width") != size[0] or harmony.get("height") != size[1]:
        raise QualificationError("smoke harmony dimensions do not describe the actual PNG")
    guide = request["control_image"]
    control = dispatch["controlnet"]
    depth = harmony.get("depth_guide")
    if not isinstance(depth, dict):
        raise QualificationError("smoke response has no depth-guide evidence")
    depth_required = {
        "image_sha256": guide["sha256"],
        "conditioning_scale": request["controlnet_conditioning_scale"],
        "controlnet_model": control["model"],
        "controlnet_revision": control["revision"],
        "controlnet_config_sha256": control["config_sha256"],
        "controlnet_quantization": control["quantization"],
        "control_guidance_start": 0.0,
        "control_guidance_end": 1.0,
    }
    for key, value in depth_required.items():
        if depth.get(key) != value:
            raise QualificationError(f"smoke depth-guide evidence {key} does not match")
    if harmony.get("generation_settings") != _generation_settings(dispatch["owner_variant"]):
        raise QualificationError("actual generation settings do not match qualification")
    _verify_grant(harmony.get("fleet_grant"), "fleet grant", dispatch)
    _verify_grant(harmony.get("local_grant"), "local grant", dispatch)
    if "exclusive_device" in dispatch["owner_variant"]:
        leases = harmony.get("device_leases")
        grants = harmony.get("device_grants")
        if (not isinstance(leases, list) or not isinstance(grants, list)
                or len(leases) != len(expected["device_ids"]) or len(grants) != len(leases)):
            raise QualificationError("exclusive smoke response must cover every configured device")
        for lease, grant, device_id in zip(leases, grants, expected["device_ids"]):
            if (lease.get("lease_id") != grant.get("lease_id")
                    or lease.get("device_id") != device_id or grant.get("device_id") != device_id
                    or grant.get("state") != "exclusive"):
                raise QualificationError("exclusive smoke device lease evidence does not match")
    return png


def smoke(dispatch_path, request_path):
    dispatch = _load_dispatch(dispatch_path)
    _verify_assets(dispatch)
    for role, record in dispatch["processes"].items():
        _verify_process(record, role)
    health = _http_json(dispatch["gateway_url"], "GET", "/health", timeout=10)
    if health.get("ok") is not True or health.get("fleet") != dispatch["broker_url"]:
        raise QualificationError("gateway health.fleet does not match the isolated broker")
    peers = _http_json(dispatch["broker_url"], "GET", "/peers", timeout=10)
    _validate_peers(peers, dispatch)
    fleet = _http_json(dispatch["broker_url"], "GET", "/fleet", timeout=30)
    _validate_fleet(fleet, dispatch)
    request = _read_json(request_path)
    _validate_smoke_request(request, dispatch)
    token = _read_token(dispatch["token_files"]["worker"], dispatch["run_dir"])
    response = _http_json(
        dispatch["gateway_url"], "POST", "/v1/images/generations", body=request,
        token=token, timeout=float(dispatch["owner_variant"].get("request_timeout_s", 1800)) + 60)
    png = _verify_smoke_response(response, request, dispatch)
    run_dir = Path(dispatch["run_dir"])
    request_bytes = Path(request_path).read_bytes()
    response_bytes = json.dumps(response, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    _write_private(run_dir / "smoke-request.json", request_bytes)
    _write_private(run_dir / "smoke-response.json", response_bytes)
    _write_private(run_dir / "smoke.png", png)
    summary = {
        "schema": SMOKE_SCHEMA,
        "run_id": dispatch["run_id"],
        "environment_fingerprint": dispatch["environment_fingerprint"],
        "request_sha256": _sha256_bytes(request_bytes),
        "response_sha256": _sha256_bytes(response_bytes),
        "request": {
            "path": str(run_dir / "smoke-request.json"),
            "sha256": _sha256_bytes(request_bytes),
        },
        "response": {
            "path": str(run_dir / "smoke-response.json"),
            "sha256": _sha256_bytes(response_bytes),
        },
        "harmony": response["harmony"],
        "output": {
            "path": str(run_dir / "smoke.png"),
            "sha256": _sha256_bytes(png),
            "width": request["width"],
            "height": request["height"],
            "nonblank": True,
        },
        "depth_guide": response["harmony"]["depth_guide"],
        "generation_settings": response["harmony"]["generation_settings"],
        "fleet_grant": response["harmony"]["fleet_grant"],
        "local_grant": response["harmony"]["local_grant"],
    }
    _assert_no_credentials(summary, (token,))
    _write_json(run_dir / "smoke.json", summary)
    return summary


def down(dispatch_path, shutdown_timeout_s=10):
    dispatch = _load_dispatch(dispatch_path)
    _verify_assets(dispatch)
    for role, record in dispatch["processes"].items():
        _verify_process(record, role, require_alive=False)
    _stop_processes(dispatch, shutdown_timeout_s=shutdown_timeout_s)
    return {"stopped": [role for role in STOP_ORDER if role in dispatch["processes"]]}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up")
    start.add_argument("--config", required=True)
    start.add_argument("--output", required=True)
    check = commands.add_parser("smoke")
    check.add_argument("--dispatch", required=True)
    check.add_argument("--request", required=True)
    stop = commands.add_parser("down")
    stop.add_argument("--dispatch", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "up":
            dispatch = up(args.config, args.output)
            print(str(Path(args.output).resolve()))
            return 0
        if args.command == "smoke":
            summary = smoke(args.dispatch, args.request)
            print(str(Path(summary["output"]["path"]).with_name("smoke.json")))
            return 0
        result = down(args.dispatch)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (QualificationError, OSError, ValueError) as exc:
        print(f"qualify-depth: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
