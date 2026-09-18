"""Strict, versioned serialization primitives for policy-lab artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_RECORD_BYTES = 64 * 1024
SUPPORTED_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_UNITS = frozenset(
    {
        "bytes",
        "bits_per_second",
        "microseconds",
        "millicores",
        "work_units_per_second",
        "tokens",
        "audio_microseconds",
        "count",
    }
)


class ContractError(ValueError):
    """A serialized policy-lab contract is invalid."""


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON value: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate object key: {key}")
        result[key] = value
    return result


def load_json(data: bytes | str, *, max_bytes: int = DEFAULT_RECORD_BYTES) -> Any:
    """Decode strict UTF-8 JSON with duplicate, nonfinite and size checks."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ContractError("max_bytes must be a positive integer")
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, bytes):
        raw = data
    else:
        raise ContractError("JSON input must be bytes or text")
    if len(raw) > max_bytes:
        raise ContractError(f"JSON byte bound exceeded: {len(raw)} > {max_bytes}")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


@dataclass(frozen=True)
class StrictObjectSpec:
    """Minimal strict-object schema with no legacy field inference."""

    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    validators: Mapping[str, Callable[[Any], Any]] | None = None

    def validate(self, value: Any, *, path: str = "record") -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ContractError(f"{path} must be an object")
        missing = self.required - value.keys()
        if missing:
            raise ContractError(f"{path} missing required fields: {sorted(missing)}")
        unknown = value.keys() - self.required - self.optional
        if unknown:
            raise ContractError(f"{path} has unknown fields: {sorted(unknown)}")
        if "schema_version" in self.required:
            version = value.get("schema_version")
            if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
                raise ContractError(f"{path} has unsupported schema_version: {version!r}")
        for name, validator in (self.validators or {}).items():
            if name in value:
                validator(value[name])
        return value


def _nonempty_string(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        raise ContractError("expected bounded non-empty string")
    return value


def _positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ContractError("expected positive integer")
    return value


def validate_quantity(value: Any) -> dict[str, Any]:
    spec = StrictObjectSpec(required=frozenset({"value", "unit"}))
    result = spec.validate(value, path="quantity")
    number = result["value"]
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise ContractError("quantity value must be numeric")
    if not math.isfinite(number) or number < 0:
        raise ContractError("quantity value must be finite and nonnegative")
    if result["unit"] not in ALLOWED_UNITS:
        raise ContractError(f"invalid unit: {result['unit']!r}")
    return result


MANIFEST_SPEC = StrictObjectSpec(
    required=frozenset({"schema_version", "kind", "max_record_bytes", "artifacts"})
)
ARTIFACT_SPEC = StrictObjectSpec(
    required=frozenset({"artifact_id", "kind", "sha256", "byte_size"})
)


def validate_artifact_manifest(
    manifest: Any,
    referenced: Mapping[str, bytes],
    *,
    hard_max_record_bytes: int = DEFAULT_RECORD_BYTES,
) -> dict[str, Any]:
    """Validate an exact content-addressed manifest and its referenced bytes."""

    result = MANIFEST_SPEC.validate(manifest, path="manifest")
    if result["kind"] != "artifact_manifest":
        raise ContractError("manifest kind must be artifact_manifest")
    declared_bound = _positive_int(result["max_record_bytes"])
    if declared_bound > hard_max_record_bytes:
        raise ContractError("declared max_record_bytes exceeds hard bound")
    try:
        encoded = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError(f"manifest is not canonical JSON: {exc}") from exc
    if len(encoded) > declared_bound:
        raise ContractError("manifest exceeds declared max_record_bytes")

    artifacts = result["artifacts"]
    if not isinstance(artifacts, list):
        raise ContractError("manifest artifacts must be a list")
    seen: set[str] = set()
    for index, item in enumerate(artifacts):
        entry = ARTIFACT_SPEC.validate(item, path=f"artifacts[{index}]")
        artifact_id = _nonempty_string(entry["artifact_id"])
        _nonempty_string(entry["kind"])
        if artifact_id in seen:
            raise ContractError(f"duplicate artifact_id: {artifact_id}")
        seen.add(artifact_id)
        digest = entry["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ContractError(f"artifacts[{index}] has invalid sha256")
        byte_size = entry["byte_size"]
        if type(byte_size) is not int or byte_size < 0:
            raise ContractError(f"artifacts[{index}] has invalid byte_size")
        if artifact_id not in referenced:
            raise ContractError(f"missing referenced artifact: {artifact_id}")
        content = referenced[artifact_id]
        if not isinstance(content, bytes):
            raise ContractError(f"artifact {artifact_id} must be bytes")
        if len(content) != byte_size:
            raise ContractError(f"artifact size mismatch: {artifact_id}")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ContractError(f"artifact digest mismatch: {artifact_id}")
    extras = set(referenced) - seen
    if extras:
        raise ContractError(f"unreferenced artifact: {sorted(extras)}")
    return result
