import hashlib
import json

import pytest

from livestack_node.policy_lab.contracts import (
    ContractError,
    StrictObjectSpec,
    load_json,
    validate_artifact_manifest,
    validate_quantity,
)


def _manifest(blob=b"profile bytes"):
    return {
        "schema_version": 1,
        "kind": "artifact_manifest",
        "max_record_bytes": 65536,
        "artifacts": [
            {
                "artifact_id": "profile-pack",
                "kind": "performance_profiles",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "byte_size": len(blob),
            }
        ],
    }


def test_strict_json_rejects_duplicate_keys_nonfinite_and_oversize():
    with pytest.raises(ContractError, match="duplicate object key"):
        load_json(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ContractError, match="non-finite"):
        load_json(b'{"value":NaN}')
    with pytest.raises(ContractError, match="byte bound"):
        load_json(b'{"value":1}', max_bytes=5)


def test_object_spec_rejects_unknown_version_and_fields():
    spec = StrictObjectSpec(required=frozenset({"schema_version", "kind"}))
    assert spec.validate({"schema_version": 1, "kind": "example"})["kind"] == "example"
    with pytest.raises(ContractError, match="unsupported schema_version"):
        spec.validate({"schema_version": 2, "kind": "example"})
    with pytest.raises(ContractError, match="unknown fields"):
        spec.validate({"schema_version": 1, "kind": "example", "legacy": True})


def test_quantities_use_explicit_valid_units_and_finite_nonnegative_values():
    assert validate_quantity({"value": 5, "unit": "bytes"}) == {"value": 5, "unit": "bytes"}
    for quantity in (
        {"value": 5, "unit": "MB"},
        {"value": -1, "unit": "bytes"},
        {"value": float("inf"), "unit": "microseconds"},
        {"value": True, "unit": "bytes"},
    ):
        with pytest.raises(ContractError):
            validate_quantity(quantity)


def test_manifest_verifies_hash_size_and_missing_references():
    blob = b"profile bytes"
    manifest = _manifest(blob)
    validated = validate_artifact_manifest(manifest, {"profile-pack": blob})
    assert validated["artifacts"][0]["artifact_id"] == "profile-pack"

    with pytest.raises(ContractError, match="missing referenced artifact"):
        validate_artifact_manifest(manifest, {})
    with pytest.raises(ContractError, match="digest mismatch"):
        validate_artifact_manifest(manifest, {"profile-pack": b"PROFILE BYTES"})
    with pytest.raises(ContractError, match="unreferenced artifact"):
        validate_artifact_manifest(manifest, {"profile-pack": blob, "extra": b"x"})


def test_manifest_rejects_unknown_nested_fields_and_invalid_digest():
    blob = b"profile bytes"
    manifest = _manifest(blob)
    manifest["artifacts"][0]["path"] = "/private/file"
    with pytest.raises(ContractError, match="unknown fields"):
        validate_artifact_manifest(manifest, {"profile-pack": blob})

    manifest = _manifest(blob)
    manifest["artifacts"][0]["sha256"] = "not-a-digest"
    with pytest.raises(ContractError, match="sha256"):
        validate_artifact_manifest(manifest, {"profile-pack": blob})


def test_serialized_manifest_itself_obeys_declared_record_bound():
    blob = b"profile bytes"
    manifest = _manifest(blob)
    manifest["max_record_bytes"] = 10
    with pytest.raises(ContractError, match="declared max_record_bytes"):
        validate_artifact_manifest(manifest, {"profile-pack": blob})

    encoded = json.dumps(_manifest(blob), separators=(",", ":")).encode()
    assert load_json(encoded)["kind"] == "artifact_manifest"
