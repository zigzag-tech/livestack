"""Image ingress resolving semantic requirements through the fleet planner."""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI, Body, Header, HTTPException
from livestack_node import transport
from livestack_node.client import exclusive_lease

from .contract import resolve_worker, validate_request


_GENERATION_SETTINGS = (
    "steps", "true_cfg_scale", "guidance_scale", "negative_prompt", "text_encoder",
    "max_sequence_length", "quantization_backend",
)


def json_call(base: str, method: str, path: str, body=None, token=None, timeout=1200, lease_id=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if lease_id:
        headers["X-Harmony-Device-Lease"] = lease_id
    try:
        status, _, raw = transport.dial(base, method, path, headers=headers,
                                      body=json.dumps(body).encode() if body is not None else None, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Harmony HTTP {e.code}: {e.read().decode()[:500]}") from e
    if status >= 400:
        raise RuntimeError(f"Harmony HTTP {status}: {raw.decode()[:500]}")
    return json.loads(raw)


def _verify_guided_result(request, actual, target):
    requirements = request["harmony_requires"]
    if (actual.get("model") != requirements["model"]
            or actual.get("revision") != requirements["revision"]):
        raise RuntimeError("worker result does not match the requested base model/revision")
    if (actual.get("width") != request["width"]
            or actual.get("height") != request["height"]):
        raise RuntimeError("worker result dimensions do not match the requested output")

    evidence = actual.get("depth_guide")
    if not isinstance(evidence, dict):
        raise RuntimeError("worker result is missing depth guidance evidence")
    attributes = target["unit"].get("attributes", {})
    if ("controlnet_quantization" not in attributes
            or evidence.get("controlnet_quantization") != attributes["controlnet_quantization"]):
        raise RuntimeError("worker depth guidance evidence mismatch: controlnet_quantization")
    if "offload" in attributes and actual.get("offload") != attributes["offload"]:
        raise RuntimeError("worker offload evidence mismatch")
    expected = {
        "image_sha256": request["control_image"]["sha256"],
        "conditioning_scale": request["controlnet_conditioning_scale"],
        "controlnet_model": requirements["controlnet_model"],
        "controlnet_revision": requirements["controlnet_revision"],
        "controlnet_config_sha256": requirements["controlnet_config_sha256"],
        "control_guidance_start": 0.0,
        "control_guidance_end": 1.0,
    }
    for key, want in expected.items():
        got = evidence.get(key)
        if got != want or (key == "conditioning_scale" and isinstance(got, bool)):
            raise RuntimeError(f"worker depth guidance evidence mismatch: {key}")

    settings = actual.get("generation_settings")
    if not isinstance(settings, dict):
        raise RuntimeError("worker result is missing generation settings")
    for key in _GENERATION_SETTINGS:
        if key in attributes and settings.get(key) != attributes[key]:
            raise RuntimeError(f"worker generation setting mismatch: {key}")


def create_app(config=None, call=json_call):
    config = config or json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    worker_token = Path(config["worker_token_file"]).read_text().strip()
    fleet_token = Path(config["fleet_token_file"]).read_text().strip()
    request_timeout = config.get("request_timeout_s", 1200)
    if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or request_timeout <= 0:
        raise ValueError("request_timeout_s must be positive")
    device_options = config.get("exclusive_device")
    device_ids = config.get("device_ids", [config["device_id"]] if config.get("device_id") else [])
    if (not isinstance(device_ids, list)
            or any(not isinstance(value, str) or not value for value in device_ids)
            or len(set(device_ids)) != len(device_ids)):
        raise ValueError("device_ids must be a unique list")
    if device_options is not None:
        if not device_ids:
            raise ValueError("exclusive_device requires at least one device_id")
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
    app = FastAPI(title="Harmony requirement-routed image generation")

    @app.get("/health")
    def health():
        return {"ok": True, "fleet": config["fleet"]}

    @app.post("/v1/images/generations")
    def generate(body: dict = Body(...), authorization: str | None = Header(None)):
        if not secrets.compare_digest(authorization or "", f"Bearer {worker_token}"):
            raise HTTPException(401, "image ingress credential required")
        try:
            request = validate_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        lease_context = lambda: nullcontext([])
        if device_options is not None:
            def lease_context():
                with ExitStack() as stack:
                    grants = []
                    minimum = device_options["min_free_bytes"]
                    for device_id in device_ids:
                        grants.append(stack.enter_context(exclusive_lease(
                            "imagegen", owner_id=config.get("owner", "acct_harmony-image"),
                            token=fleet_token, brokers=[config["fleet"]], device_id=device_id,
                            min_free_bytes=minimum.get(device_id) if isinstance(minimum, dict) else minimum,
                            wait_timeout_s=device_options.get("wait_timeout_s", 240),
                            ttl_seconds=device_options.get("ttl_seconds", 120),
                            request_id=f"image-gateway-{secrets.token_hex(16)}")))
                    yield grants
            lease_context = contextmanager(lease_context)
        try:
            with lease_context() as device_grants:
                # One primary placement can hold several same-owner device leases.
                admission = {
                    "requires": request["harmony_requires"],
                    "owner": config.get("owner", "acct_harmony-image"),
                }
                lease_ids = [grant["lease_id"] for grant in device_grants]
                if lease_ids:
                    admission["device_lease_ids"] = lease_ids
                grant = call(config["fleet"], "POST", "/admit", admission, token=fleet_token)
                view = call(config["fleet"], "GET", "/fleet")
                target = resolve_worker(view, grant, request["harmony_requires"])
                endpoint = target["peer"].removesuffix("/livestack")
                lease_evidence = json.dumps(lease_ids, separators=(",", ":")) if lease_ids else None
                result = call(endpoint, "POST", "/v1/images/generations", request,
                              token=worker_token, timeout=request_timeout,
                              lease_id=lease_evidence) if lease_evidence else call(
                                  endpoint, "POST", "/v1/images/generations", request,
                                  token=worker_token, timeout=request_timeout)
                actual = result.get("harmony", {})
                if actual.get("unit") != grant["kind"] or actual.get("device_id") != grant["device_id"]:
                    raise RuntimeError("worker result does not match the fleet grant")
                if lease_ids and actual.get("device_leases") != [
                        {"lease_id": value, "device_id": device}
                        for value, device in zip(lease_ids, device_ids)]:
                    raise RuntimeError("worker result does not match every device lease")
                if request["harmony_requires"].get("depth_guide") is True:
                    _verify_guided_result(request, actual, target)
                actual["fleet_grant"] = grant
                if device_grants:
                    actual["device_grants"] = device_grants
                actual["worker_endpoint"] = endpoint
                return result
        except (RuntimeError, OSError, ValueError) as e:
            raise HTTPException(503, str(e)) from e
    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    uvicorn.run(create_app(config), host="0.0.0.0", port=config.get("port", 8211))


if __name__ == "__main__":
    main()
