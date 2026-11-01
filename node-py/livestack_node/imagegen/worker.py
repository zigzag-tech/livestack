"""A reusable image worker advertising one model as a Harmony managed unit."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Body, Header, HTTPException
from livestack_node import attach, counting
from livestack_node.client import admit
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .contract import satisfies, validate_request
from .runtime import (CONTROLNET_CLASSES, FLUX2_IMAGE_MODEL, QWEN_IMAGE_MODEL,
                      SDXL_IMAGE_MODEL, DepthConfigurationError, ImageRuntime,
                      validate_controlnet_quantization, validate_depth_variant,
                      validate_offload)
from .sdxl import SDXLImageRuntime
from .flux2 import Flux2ImageRuntime


_FULL_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _bounded_log_value(value, limit=128):
    return None if value is None else str(value)[:limit]


def _log_depth_verification(config, depth, status, reason=None):
    depth = depth if isinstance(depth, dict) else {}
    weight_bytes = depth.get("weight_bytes")
    record = {
        "event": "image_depth_startup",
        "status": status,
        "model": _bounded_log_value(config.get("model")),
        "revision": _bounded_log_value(config.get("revision")),
        "unit": _bounded_log_value(config.get("unit")),
        "quantization": _bounded_log_value(config.get("quantization", "nf4"), 32),
        "controlnet_model": _bounded_log_value(depth.get("model")),
        "controlnet_revision": _bounded_log_value(depth.get("revision")),
        "controlnet_quantization": _bounded_log_value(depth.get("quantization", "bf16"), 32),
        "config_sha256": _bounded_log_value(depth.get("config_sha256")),
        "weight_sha256": _bounded_log_value(depth.get("weight_sha256")),
        "weight_bytes": weight_bytes if type(weight_bytes) is int and 0 <= weight_bytes < 2**63 else None,
    }
    if reason is not None:
        record["reason"] = _bounded_log_value(reason, 240)
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))


def _required_revision(value, name):
    if not isinstance(value, str) or _FULL_REVISION.fullmatch(value) is None:
        raise DepthConfigurationError(f"{name} must be a full lowercase revision")
    return value


def _required_sha256(value, name):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DepthConfigurationError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_depth_startup(config):
    depth = config.get("depth_guide")
    try:
        if not isinstance(depth, dict):
            raise DepthConfigurationError("depth_guide must be a configuration object")
        validate_depth_variant(config.get("model"), config.get("unit"),
                               config.get("quantization", "nf4"))
        validate_controlnet_quantization(depth)
        base_revision = _required_revision(config.get("revision"), "base revision")
        base_snapshot = config.get("snapshot")
        if not isinstance(base_snapshot, str) or Path(base_snapshot).name != base_revision:
            raise DepthConfigurationError("base snapshot basename must equal the base revision")
        if type(config.get("resident_bytes")) is not int or config["resident_bytes"] < 0:
            raise DepthConfigurationError("depth resident_bytes must be a nonnegative integer")

        if not isinstance(depth.get("model"), str) or not depth["model"]:
            raise DepthConfigurationError("ControlNet model must be a nonempty string")
        control_revision = _required_revision(depth.get("revision"), "ControlNet revision")
        control_snapshot = depth.get("snapshot")
        if not isinstance(control_snapshot, str) or Path(control_snapshot).name != control_revision:
            raise DepthConfigurationError("ControlNet snapshot basename must equal its revision")
        config_sha256 = _required_sha256(depth.get("config_sha256"), "ControlNet config_sha256")
        weight_sha256 = _required_sha256(depth.get("weight_sha256"), "ControlNet weight_sha256")
        weight_bytes = depth.get("weight_bytes")
        if type(weight_bytes) is not int or weight_bytes <= 0:
            raise DepthConfigurationError("ControlNet weight_bytes must be a positive integer")

        snapshot = Path(control_snapshot)
        config_path = snapshot / "config.json"
        raw_config = config_path.read_bytes()
        if hashlib.sha256(raw_config).hexdigest() != config_sha256:
            raise DepthConfigurationError("ControlNet config.json SHA-256 mismatch")
        try:
            control_config = json.loads(raw_config)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DepthConfigurationError("ControlNet config.json is malformed") from exc
        expected_class = CONTROLNET_CLASSES.get(config.get("model"))
        if (not isinstance(control_config, dict)
                or control_config.get("_class_name") != expected_class):
            raise DepthConfigurationError("ControlNet config.json has the wrong _class_name")

        weight_path = snapshot / "diffusion_pytorch_model.safetensors"
        if weight_path.stat().st_size != weight_bytes:
            raise DepthConfigurationError("ControlNet weight byte count mismatch")
        if _file_sha256(weight_path) != weight_sha256:
            raise DepthConfigurationError("ControlNet weight SHA-256 mismatch")
    except DepthConfigurationError as exc:
        _log_depth_verification(config, depth, "refused", str(exc))
        raise
    except (OSError, TypeError, ValueError) as exc:
        error = DepthConfigurationError(f"ControlNet startup verification failed: {exc}")
        _log_depth_verification(config, depth, "refused", str(error))
        raise error from exc

    _log_depth_verification(config, depth, "verified")
    return weight_bytes if depth.get("quantization", "bf16") == "bf16" else (weight_bytes + 1) // 2


def create_app(config: dict | None = None, runtime_factory=ImageRuntime):
    config = config or json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    depth = config.get("depth_guide")
    offload = validate_offload(config.get("offload"))
    device_options = config.get("exclusive_device")
    device_ids = config.get("device_ids", [config["device_id"]])
    if (not isinstance(device_ids, list) or not device_ids
            or any(not isinstance(value, str) or not value for value in device_ids)
            or len(set(device_ids)) != len(device_ids)
            or device_ids[0] != config["device_id"]):
        raise ValueError("device_ids must be unique and start with the primary device_id")
    if device_options is not None:
        if not isinstance(device_options, dict):
            raise ValueError("exclusive_device must be a configuration object")
        minimum = device_options.get("min_free_bytes")
        if isinstance(minimum, dict):
            if (set(minimum) != set(device_ids)
                    or any(type(value) is not int or value <= 0 for value in minimum.values())):
                raise ValueError("exclusive_device.min_free_bytes must positively cover every device_id")
        elif type(minimum) is not int or minimum <= 0:
            raise ValueError("exclusive_device.min_free_bytes must be positive per device")
        for key in ("wait_timeout_s", "ttl_seconds"):
            value = device_options.get(key, 240 if key == "wait_timeout_s" else 120)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"exclusive_device.{key} must be positive")
    footprint = config["resident_bytes"]
    if depth is not None:
        controlnet_bytes = _verify_depth_startup(config)
        if len(device_ids) == 1 and config["model"] != FLUX2_IMAGE_MODEL:
            footprint += controlnet_bytes
    token = Path(config["token_file"]).read_text().strip()
    fleet_token = (Path(config["fleet_token_file"]).read_text().strip()
                   if config.get("fleet_token_file") else None)
    attrs = {"class": "imagegen", "task": "text_to_image", "params_b": config["params_b"],
             "model": config["model"], "license": "apache-2.0", "quantization": config.get("quantization", "nf4")}
    if config.get("revision") is not None:
        attrs["revision"] = config["revision"]
    if config["model"] == QWEN_IMAGE_MODEL:
        attrs.update(steps=config["steps"],
                     true_cfg_scale=config.get("true_cfg_scale", 4.0),
                     negative_prompt=config.get("negative_prompt", ""),
                     text_encoder="CPU FP32",
                     max_sequence_length=config.get("max_sequence_length", 512))
    elif config["model"] in (SDXL_IMAGE_MODEL, FLUX2_IMAGE_MODEL):
        attrs.update(steps=config["steps"],
                     guidance_scale=config.get("guidance_scale", 7.5 if config["model"] == SDXL_IMAGE_MODEL else 4.0),
                     negative_prompt=config.get("negative_prompt", "blurry, low quality, distorted geometry" if config["model"] == SDXL_IMAGE_MODEL else ""),
                     text_encoder="CPU FP32" if config["model"] == SDXL_IMAGE_MODEL else "CPU INT8")
    if config["model"] == FLUX2_IMAGE_MODEL:
        attrs["quantization_backend"] = config.get("quantization_backend", "bitsandbytes")
    if offload is not None:
        attrs["offload"] = offload
    if depth is not None:
        attrs.update(depth_guide=True, controlnet_model=depth["model"],
                     controlnet_revision=depth["revision"],
                     controlnet_config_sha256=depth["config_sha256"],
                     controlnet_quantization=depth.get("quantization", "bf16"))
    unit_name = config["unit"]
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-gpu")
    holder, busy = {}, counting()
    request_lock = threading.Lock()
    runtime_classes = {SDXL_IMAGE_MODEL: SDXLImageRuntime, FLUX2_IMAGE_MODEL: Flux2ImageRuntime}
    selected_runtime = (runtime_classes.get(config["model"], runtime_factory)
                        if runtime_factory is ImageRuntime else runtime_factory)

    def gpu_call(fn):
        return executor.submit(fn).result()

    def load():
        started = time.monotonic()
        options = dict(steps=config["steps"], threads=config.get("cpu_threads", 8),
                       quantization=config.get("quantization", "nf4"))
        if config["model"] == QWEN_IMAGE_MODEL:
            options.update(true_cfg_scale=config.get("true_cfg_scale", 4.0),
                           negative_prompt=config.get("negative_prompt", ""),
                           max_sequence_length=config.get("max_sequence_length", 512))
        elif config["model"] in (SDXL_IMAGE_MODEL, FLUX2_IMAGE_MODEL):
            options.update(guidance_scale=config.get("guidance_scale", 7.5 if config["model"] == SDXL_IMAGE_MODEL else 4.0),
                           negative_prompt=config.get("negative_prompt", "blurry, low quality, distorted geometry" if config["model"] == SDXL_IMAGE_MODEL else ""))
        if config["model"] == FLUX2_IMAGE_MODEL:
            options["videox_fun_root"] = config.get("videox_fun_root")
            options["quantization_backend"] = config.get("quantization_backend", "bitsandbytes")
        if offload is not None:
            options["offload"] = offload
        if depth is not None:
            options.update(unit=unit_name, depth_guide=depth)
        runtime = selected_runtime(config["snapshot"], config["model"], **options)
        holder["runtime"] = runtime
        holder["load_s"] = round(time.monotonic()-started, 3)
        return runtime

    def free():
        runtime = holder.pop("runtime", None)
        if runtime is not None:
            runtime.close()

    unit = ManagedUnit(unit_name, load, free, footprint=footprint,
                       residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
                       attributes=attrs, spread_group="imagegen", priority=30,
                       min_residency_s=60, reload_cost=120)
    app = FastAPI(title="Harmony image worker")
    manager, coordinator = attach(
        app, host_id=config["host_id"], kind="imagegen", units={unit_name: unit},
        idle_seconds=config.get("idle_seconds", 900), coload=True, gpu_call=gpu_call,
        port=config["port"], in_flight=busy, inventory=attrs,
        device_id=config["device_id"])

    @app.get("/health")
    def health():
        return {"ok": True, "model": config["model"], "loaded": unit.loaded}

    @app.post("/v1/images/generations")
    def generate(body: dict = Body(...), authorization: str | None = Header(None),
                 x_harmony_device_lease: str | None = Header(None)):
        if not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "image worker credential required")
        lease_ids = []
        if x_harmony_device_lease:
            try:
                lease_ids = json.loads(x_harmony_device_lease)
            except json.JSONDecodeError as exc:
                raise HTTPException(503, "malformed gateway device lease evidence") from exc
            if (not isinstance(lease_ids, list) or len(lease_ids) != len(device_ids)
                    or any(not isinstance(value, str) or not value for value in lease_ids)
                    or len(set(lease_ids)) != len(lease_ids)):
                raise HTTPException(503, "gateway device lease evidence does not match worker devices")
        if device_options is not None and not lease_ids:
            raise HTTPException(503, "exclusive worker requires an authoritative gateway device lease")
        try:
            request = validate_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not satisfies(attrs, request["harmony_requires"]):
            raise HTTPException(422, "worker does not satisfy the model requirements")
        max_dimension = config.get("max_dimension", 1024)
        if max(request["width"], request["height"]) > max_dimension:
            raise HTTPException(422, f"this worker is qualified for dimensions up to {max_dimension}px")
        # No lock on the GPU executor while admission may dispatch a warm to us.
        with request_lock, busy:
            started = time.monotonic()
            leased = coordinator.acquire_lease(unit_name, "harmony-image", ttl_seconds=3600)
            lease_id = leased.get("lease_id") if isinstance(leased, dict) else getattr(leased, "lease_id", None)
            try:
                grant = admit(unit_name, owner_id="harmony-image", token=fleet_token,
                              brokers=[config["broker"]], timeout=1200,
                              device_lease_ids=lease_ids or None)
                if not grant.get("granted") or grant.get("device_id") != config["device_id"]:
                    raise HTTPException(503, grant.get("reason") or "no concrete local Harmony grant")
                image, metrics = gpu_call(lambda: manager.run(unit_name, lambda runtime: runtime.generate(request)))
            finally:
                if lease_id:
                    coordinator.release_lease(lease_id)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            harmony = {"model": config["model"], "revision": config.get("revision"),
                       "unit": unit_name, "host": config["host_id"], "device_id": config["device_id"],
                       "device_ids": device_ids,
                       "prompt": request["prompt"], "seed": request["seed"],
                       "requirements": request["harmony_requires"], "local_grant": grant,
                       "total_s": round(time.monotonic()-started, 3),
                       "load_s": holder.get("load_s"), **metrics}
            if lease_ids:
                harmony["device_leases"] = [
                    {"lease_id": value, "device_id": device}
                    for value, device in zip(lease_ids, device_ids)]
            harmony.update(width=image.size[0], height=image.size[1])
            harmony.setdefault("depth_guide", None)
            if config["model"] == QWEN_IMAGE_MODEL:
                harmony["generation_settings"] = {
                    "steps": config["steps"],
                    "true_cfg_scale": config.get("true_cfg_scale", 4.0),
                    "negative_prompt": config.get("negative_prompt", ""),
                    "text_encoder": "CPU FP32",
                    "max_sequence_length": config.get("max_sequence_length", 512),
                }
            return {"created": int(time.time()),
                    "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}],
                    "harmony": harmony}
    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    uvicorn.run(create_app(config), host=os.environ.get("LIVESTACK_LISTEN_HOST", "0.0.0.0"), port=config["port"])


if __name__ == "__main__":
    main()
