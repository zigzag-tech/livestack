"""Capacity-bounded, dedicated storage for lab-owned immutable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ContractError


class StoreFullError(ContractError):
    pass


class ArtifactStore:
    """A small reference implementation of retention and protection rules."""

    MARKER = ".harmony-policy-lab-store"

    def __init__(
        self, root: Path, *, max_bytes: int, retention_window_us: int | None
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if retention_window_us is not None and retention_window_us < 0:
            raise ValueError("retention_window_us must be nonnegative or None")
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.retention_window_us = retention_window_us
        self.artifacts_root = self.root / "artifacts"
        self.index_path = self.root / "index.json"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self.MARKER).touch(exist_ok=True)
        self.artifacts_root.mkdir(exist_ok=True)
        self._index: dict[str, dict[str, Any]] = self._load_index()

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid artifact store index: {exc}") from exc
        if not isinstance(value, dict):
            raise ContractError("artifact store index must be an object")
        return value

    def _save(self) -> None:
        encoded = json.dumps(
            self._index,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        descriptor, name = tempfile.mkstemp(prefix="index-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.index_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _path(self, artifact_id: str) -> Path:
        if len(artifact_id) != 64 or any(char not in "0123456789abcdef" for char in artifact_id):
            raise ContractError("artifact_id must be a lowercase SHA-256 digest")
        path = (self.artifacts_root / artifact_id).resolve()
        if path.parent != self.artifacts_root:
            raise ContractError("artifact path escapes dedicated store")
        return path

    @property
    def used_bytes(self) -> int:
        return sum(int(item["byte_size"]) for item in self._index.values())

    def artifact_ids(self) -> set[str]:
        return set(self._index)

    def _prune_for(self, needed: int, *, now_us: int) -> None:
        if self.retention_window_us is None:
            return
        cutoff = now_us - self.retention_window_us
        eligible = sorted(
            (
                (artifact_id, metadata)
                for artifact_id, metadata in self._index.items()
                if metadata["created_at_us"] <= cutoff
                and not metadata["pinned"]
                and not metadata["active"]
                and not metadata["release_evidence"]
                and metadata["references"] == 0
            ),
            key=lambda item: (item[1]["created_at_us"], item[0]),
        )
        for artifact_id, _ in eligible:
            if self.used_bytes + needed <= self.max_bytes:
                break
            self._path(artifact_id).unlink(missing_ok=False)
            del self._index[artifact_id]
        self._save()

    def put(
        self,
        content: bytes,
        *,
        created_at_us: int,
        pinned: bool = False,
        active: bool = False,
        release_evidence: bool = False,
    ) -> str:
        if not isinstance(content, bytes):
            raise ContractError("artifact content must be bytes")
        if type(created_at_us) is not int or created_at_us < 0:
            raise ContractError("created_at_us must be nonnegative")
        artifact_id = hashlib.sha256(content).hexdigest()
        if artifact_id in self._index:
            return artifact_id
        self._prune_for(len(content), now_us=created_at_us)
        if self.used_bytes + len(content) > self.max_bytes:
            raise StoreFullError("lab artifact store is full; no eligible deletion exists")
        path = self._path(artifact_id)
        descriptor, name = tempfile.mkstemp(prefix="artifact-", dir=self.artifacts_root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._index[artifact_id] = {
            "byte_size": len(content),
            "created_at_us": created_at_us,
            "pinned": bool(pinned),
            "active": bool(active),
            "release_evidence": bool(release_evidence),
            "references": 0,
        }
        self._save()
        return artifact_id

    def add_reference(self, artifact_id: str) -> None:
        self._path(artifact_id)
        self._index[artifact_id]["references"] += 1
        self._save()

    def remove_reference(self, artifact_id: str) -> None:
        self._path(artifact_id)
        metadata = self._index[artifact_id]
        if metadata["release_evidence"]:
            raise ValueError("release evidence protection cannot be removed as a reference")
        if metadata["references"] <= 0:
            raise ValueError("artifact has no removable reference")
        metadata["references"] -= 1
        self._save()
