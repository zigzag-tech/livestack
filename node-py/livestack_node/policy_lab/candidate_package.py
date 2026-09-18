"""Strict candidate package boundary; evaluator assets never come from authors."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .contracts import ContractError


ALLOWED_FILES = frozenset({"manifest.json", "policy.py", "hypothesis.json"})
MAX_CANDIDATE_BYTES = 1024 * 1024


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{field} must be a SHA-256 digest")
    return value


def validate_candidate_package(
    root: Path,
    *,
    expected_profile_sha256: str,
    expected_evaluator_sha256: str,
    expected_dataset_sha256: str,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ContractError("candidate package root is unavailable")
    entries = list(root.iterdir())
    if any(entry.is_symlink() for entry in entries):
        raise ContractError("candidate package symlinks are forbidden")
    if any(not entry.is_file() or entry.resolve().parent != root for entry in entries):
        raise ContractError("candidate package may contain only regular top-level files")
    names = {entry.name for entry in entries}
    if names != ALLOWED_FILES:
        raise ContractError("candidate package has missing or extra files")
    if sum(entry.stat().st_size for entry in entries) > MAX_CANDIDATE_BYTES:
        raise ContractError("candidate package exceeds output limit")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("candidate manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("kind") != "candidate_package":
        raise ContractError("candidate manifest has wrong schema or kind")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"policy.py", "hypothesis.json"}:
        raise ContractError("candidate file manifest is incomplete")
    for name, expected in files.items():
        _digest(expected, f"files.{name}")
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ContractError(f"candidate file digest mismatch: {name}")
    for field, expected in (
        ("profile_sha256", expected_profile_sha256),
        ("evaluator_sha256", expected_evaluator_sha256),
        ("dataset_sha256", expected_dataset_sha256),
    ):
        if _digest(manifest.get(field), field) != _digest(expected, f"expected {field}"):
            raise ContractError(f"candidate attempted to replace immutable {field}")
    try:
        tree = ast.parse((root / "policy.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ContractError("candidate policy source is invalid") from exc
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)):
        raise ContractError("candidate policy imports and private-network access are forbidden")
    hypothesis = json.loads((root / "hypothesis.json").read_text(encoding="utf-8"))
    if not isinstance(hypothesis, dict) or set(hypothesis) != {"hypothesis", "expected_tradeoff"}:
        raise ContractError("candidate hypothesis artifact is invalid")
    if any(not isinstance(value, str) or not value for value in hypothesis.values()):
        raise ContractError("candidate hypothesis fields must be non-empty")
    return {
        "schema_version": 1,
        "kind": "candidate_validation_report",
        "candidate_id": manifest.get("candidate_id"),
        "status": "valid",
        "package_sha256": hashlib.sha256(
            b"".join((root / name).read_bytes() for name in sorted(names))
        ).hexdigest(),
        "network_allowed": False,
        "immutable_evaluator_inputs": True,
    }
