"""Validation for ``jingway.perception.v1``.

The JSON schema is generated from Jingway's public Zod contract. These checks
add relational invariants JSON Schema cannot express: image identity, request
correlation, and source-pixel bounds.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "jingway.perception.v1"
_SCHEMAS = json.loads((Path(__file__).parent / "data" / "schema.json").read_text())


class PerceptionContractError(ValueError):
    def __init__(self, cause: str, detail: str, status: int = 400, *, retryable: bool = False):
        super().__init__(detail)
        self.cause = cause
        self.detail = detail
        self.status = status
        self.retryable = retryable

    def envelope(self, request_id: str) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "requestId": request_id or "unknown",
            "outcome": "error",
            "cause": self.cause,
            "retryable": self.retryable,
            "detail": self.detail,
        }


def _jsonschema_validate(kind: str, value: Mapping[str, Any]) -> None:
    try:
        import jsonschema
        jsonschema.Draft202012Validator(_SCHEMAS[kind]).validate(dict(value))
    except ImportError as exc:
        raise PerceptionContractError("unavailable", "jsonschema is required", 503, retryable=True) from exc
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        where = f" at {path}" if path else ""
        raise PerceptionContractError("invalid_input" if kind == "request" else "invalid_output",
                                      f"{exc.message}{where}", 422) from exc


def validate_request(value: Mapping[str, Any]) -> dict:
    _jsonschema_validate("request", value)
    request = dict(value)
    image_ids = [image["id"] for image in request["images"]]
    if len(image_ids) != len(set(image_ids)):
        raise PerceptionContractError("invalid_input", "image ids must be unique", 422)
    for image in request["images"]:
        transports = int(bool(image.get("objectRef"))) + int(bool(image.get("contentBase64")))
        if transports != 1:
            raise PerceptionContractError(
                "invalid_input", "each image requires exactly one of objectRef or contentBase64", 422)
    if request["task"]["type"] == "segmentation":
        known = set(image_ids)
        for prompt in request["task"]["prompts"]:
            if prompt["type"] == "exemplar" and prompt["imageId"] not in known:
                raise PerceptionContractError("invalid_input", "exemplar references an unknown image", 422)
    return request


def _points(geometry: Mapping[str, Any]):
    kind = geometry["kind"]
    if kind == "point":
        yield geometry["point"]
    elif kind == "box":
        box = geometry["box"]
        yield {"x": box["xMin"], "y": box["yMin"]}
        yield {"x": box["xMax"], "y": box["yMax"]}
    elif kind == "polygon":
        polygon = geometry["polygon"]
        yield from polygon["exterior"]
        for hole in polygon.get("holes", []):
            yield from hole


def validate_result(value: Mapping[str, Any], *, request: Mapping[str, Any]) -> dict:
    _jsonschema_validate("result", value)
    result = dict(value)
    if result["requestId"] != request["requestId"]:
        raise PerceptionContractError("invalid_output", "requestId does not match request", 422)
    images = {image["id"]: image for image in request["images"]}
    observation_ids: set[str] = set()
    for observation in result["observations"]:
        oid = observation["id"]
        if oid in observation_ids:
            raise PerceptionContractError("invalid_output", "observation ids must be unique", 422)
        observation_ids.add(oid)
        image = images.get(observation["imageId"])
        if image is None:
            raise PerceptionContractError("invalid_output", "observation references an unknown image", 422)
        geometry = observation.get("geometry")
        if not geometry:
            continue
        if geometry["kind"] == "mask":
            mask = geometry["mask"]
            if mask["width"] != image["width"] or mask["height"] != image["height"]:
                raise PerceptionContractError("invalid_output", "mask dimensions must equal source image dimensions", 422)
            continue
        for point in _points(geometry):
            if point["x"] > image["width"] or point["y"] > image["height"]:
                raise PerceptionContractError("invalid_output", "geometry lies outside source image bounds", 422)
    if result["outcome"] == "empty" and result["observations"]:
        raise PerceptionContractError("invalid_output", "empty outcome cannot contain observations", 422)
    return result
