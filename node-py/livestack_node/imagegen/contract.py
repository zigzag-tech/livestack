"""Small, bounded image request contract shared by ingress and workers."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import re

from livestack_node.planner import Unit, _unit_satisfies


MAX_CONTROL_IMAGE_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_GUIDED_REQUIREMENTS = (
    "model", "revision", "controlnet_model", "controlnet_revision",
    "controlnet_config_sha256", "quantization",
)


def _validate_png(image_field, width: int, height: int, name: str = "control_image") -> None:
    if not isinstance(image_field, dict):
        raise ValueError(f"{name} must be a JSON object")
    if image_field.get("media_type") != "image/png":
        raise ValueError(f"{name}.media_type must be image/png")
    encoded = image_field.get("b64_json")
    if not isinstance(encoded, str):
        raise ValueError(f"{name}.b64_json must be a base64 string")
    if len(encoded) > 4 * ((MAX_CONTROL_IMAGE_BYTES + 2) // 3):
        raise ValueError(f"{name} exceeds the 8 MiB decoded limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name}.b64_json must be strict base64") from exc
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise ValueError(f"{name}.b64_json must be strict base64")
    if len(decoded) > MAX_CONTROL_IMAGE_BYTES:
        raise ValueError(f"{name} exceeds the 8 MiB decoded limit")
    digest = image_field.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{name}.sha256 must be 64 lowercase hexadecimal characters")
    if hashlib.sha256(decoded).hexdigest() != digest:
        raise ValueError(f"{name} SHA-256 mismatch")

    from PIL import Image
    try:
        image = Image.open(io.BytesIO(decoded))
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"{name} must be one RGB-readable PNG") from exc
    with image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError(f"{name} must contain exactly one PNG image")
        if image.size != (width, height):
            raise ValueError(f"{name} dimensions must equal the requested width and height")
        try:
            image.load()
            with image.convert("RGB") as rgb:
                rgb.load()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
            raise ValueError(f"{name} must be one RGB-readable PNG") from exc


def _validate_guided_requirements(req: dict) -> None:
    missing = [key for key in _GUIDED_REQUIREMENTS if key not in req]
    if missing:
        raise ValueError(f"guided harmony_requires is missing exact identity: {', '.join(missing)}")
    for key in ("model", "controlnet_model", "quantization"):
        if not isinstance(req[key], str) or not req[key]:
            raise ValueError(f"guided harmony_requires.{key} must be a nonempty string")
    for key in ("revision", "controlnet_revision"):
        if not isinstance(req[key], str) or _REVISION.fullmatch(req[key]) is None:
            raise ValueError(f"guided harmony_requires.{key} must be a full lowercase revision")
    if (not isinstance(req["controlnet_config_sha256"], str)
            or _SHA256.fullmatch(req["controlnet_config_sha256"]) is None):
        raise ValueError("guided harmony_requires.controlnet_config_sha256 must be lowercase SHA-256")


def validate_request(body: dict) -> dict:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("prompt must be a nonempty string of at most 12000 characters")
    dimensions = {}
    for key, default in (("width", 1024), ("height", 1024)):
        value = body.get(key, default)
        if type(value) is not int or not 256 <= value <= 2048 or value % 16:
            raise ValueError(f"{key} must be a multiple of 16 between 256 and 2048")
        dimensions[key] = value
    seed = body.get("seed", 42)
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed must be an integer between 0 and 2**63-1")
    # How long the caller can wait for Harmony to find room (seconds): a busy or just-restarted fleet is not a refusal.
    wait = body.get("harmony_wait_s", 0)
    if type(wait) not in (int, float) or not 0 <= wait <= 3600:
        raise ValueError("harmony_wait_s must be a number of seconds between 0 and 3600")
    req = body.get("harmony_requires", {})
    if not isinstance(req, dict) or req.get("class", "imagegen") != "imagegen":
        raise ValueError("harmony_requires must describe class=imagegen")
    # Inpainting is derived from the request, not declared: an `init_image` and a `mask_image` (PNGs of the requested
    # size; white in the mask is repainted, black kept) make it task=inpaint, served only by units declaring `inpaint`.
    # Outpainting is the same request with blank canvas under a white mask.
    inpaint_keys = ("init_image" in body, "mask_image" in body)
    if any(inpaint_keys) and not all(inpaint_keys):
        raise ValueError("init_image and mask_image must be paired")
    task = "inpaint" if all(inpaint_keys) else "text_to_image"
    if req.get("task", task) != task:
        raise ValueError(f"harmony_requires must describe task={task}" + (" (send init_image and mask_image to inpaint)" if req.get("task") == "inpaint" else ""))
    if "inpaint" in req and req["inpaint"] is not (task == "inpaint"):
        raise ValueError("harmony_requires.inpaint is derived from init_image and mask_image; do not state it otherwise")
    if task == "inpaint" and ("control_image" in body or "depth_guide" in req):
        raise ValueError("an inpaint request cannot also be depth-guided")
    if "depth_guide" in req and req["depth_guide"] is not True:
        raise ValueError("harmony_requires.depth_guide must be literal JSON true")

    guided_keys = ("control_image" in body, "controlnet_conditioning_scale" in body,
                   "depth_guide" in req)
    if any(guided_keys) and not all(guided_keys):
        raise ValueError("control_image, controlnet_conditioning_scale, and depth_guide must be paired")
    if all(guided_keys):
        scale = body["controlnet_conditioning_scale"]
        valid_scale = not isinstance(scale, bool) and isinstance(scale, (int, float))
        if valid_scale:
            try:
                valid_scale = math.isfinite(scale) and 0.8 <= scale <= 1.0
            except OverflowError:
                valid_scale = False
        if not valid_scale:
            raise ValueError("controlnet_conditioning_scale must be a finite number from 0.8 through 1.0")
        _validate_guided_requirements(req)
        _validate_png(body["control_image"], dimensions["width"], dimensions["height"])

    out = dict(body)
    out.update(dimensions)
    out["seed"] = seed
    out["harmony_wait_s"] = wait
    if task == "inpaint":
        for name in ("init_image", "mask_image"):
            _validate_png(body[name], dimensions["width"], dimensions["height"], name)
    out["harmony_requires"] = {"class": "imagegen", **req, "task": task, **({"inpaint": True} if task == "inpaint" else {})}
    return out


def satisfies(attributes: dict, requirements: dict) -> bool:
    return _unit_satisfies(Unit(kind="candidate", footprint={}, attributes=attributes), requirements)


def resolve_worker(view: dict, grant: dict, requirements: dict) -> dict:
    """Resolve the planner's grant, verifying its advertised requirements again."""
    if not grant.get("granted") or not grant.get("device_id") or not grant.get("kind"):
        # The planner says WHY it deferred (`defer_reason`); a bare "could not place it" leaves the caller guessing between
        # a registration gap, a full device and a refusal to respect.
        why = grant.get("defer_reason")
        raise RuntimeError((grant.get("reason") or "Harmony did not grant a concrete image unit") + (f": {why}" if why else ""))
    matches = []
    for host, data in view.get("hosts", {}).items():
        for node in data.get("nodes", []):
            if node.get("device_id") != grant["device_id"] or node.get("state") != "fresh":
                continue
            for unit in node.get("units", []):
                if unit["kind"] == grant["kind"] and satisfies(unit.get("attributes", {}), requirements):
                    matches.append({"host": host, "peer": node["peer"], "unit": unit})
    if len(matches) != 1:
        raise RuntimeError(f"grant resolved to {len(matches)} fresh workers; refusing ambiguous routing")
    return matches[0]
