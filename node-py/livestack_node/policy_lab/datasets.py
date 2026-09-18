"""Immutable episode partitions and holdout exposure accounting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .contracts import ContractError


class DatasetError(ContractError):
    pass


PARTITIONS = frozenset({"training", "regression", "calibration", "holdout"})


@dataclass(frozen=True)
class Episode:
    episode_id: str
    session_id: str
    workflow_id: str
    time_window_id: str
    near_duplicate_hash: str

    @property
    def group_key(self) -> tuple[str, str, str]:
        return (self.session_id, self.workflow_id, self.time_window_id)


def _digest(episodes: tuple[Episode, ...], assignments: tuple[tuple[str, str], ...]) -> str:
    value = {
        "schema_version": 1,
        "kind": "dataset_manifest",
        "episodes": [episode.__dict__ for episode in episodes],
        "assignments": list(assignments),
    }
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class DatasetManifest:
    episodes: tuple[Episode, ...]
    assignments: tuple[tuple[str, str], ...]
    manifest_sha256: str

    @classmethod
    def create(
        cls, episodes: tuple[Episode, ...], *, assignments: Mapping[str, str]
    ) -> "DatasetManifest":
        if len({episode.episode_id for episode in episodes}) != len(episodes):
            raise DatasetError("duplicate episode_id")
        if set(assignments) != {episode.episode_id for episode in episodes}:
            raise DatasetError("assignments must cover every episode exactly")
        if set(assignments.values()) - PARTITIONS:
            raise DatasetError("unknown dataset partition")
        groups: dict[tuple[str, str, str], set[str]] = {}
        duplicates: dict[str, set[str]] = {}
        for episode in episodes:
            partition = assignments[episode.episode_id]
            groups.setdefault(episode.group_key, set()).add(partition)
            duplicates.setdefault(episode.near_duplicate_hash, set()).add(partition)
        if any(len(partitions) > 1 for partitions in groups.values()):
            raise DatasetError("whole episode group split across partitions")
        if any(len(partitions) > 1 for partitions in duplicates.values()):
            raise DatasetError("near-duplicate incidents split across partitions")
        ordered_episodes = tuple(sorted(episodes, key=lambda item: item.episode_id))
        ordered_assignments = tuple(sorted(assignments.items()))
        return cls(
            ordered_episodes,
            ordered_assignments,
            _digest(ordered_episodes, ordered_assignments),
        )

    def verify(self) -> None:
        if _digest(self.episodes, self.assignments) != self.manifest_sha256:
            raise DatasetError("dataset manifest hash mismatch")

    def author_visible(self) -> tuple[Episode, ...]:
        assignment = dict(self.assignments)
        return tuple(
            episode
            for episode in self.episodes
            if assignment[episode.episode_id] in {"training", "regression"}
        )


class ExposureLedger:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = {}

    def record(
        self,
        candidate_id: str,
        manifest: DatasetManifest,
        *,
        partition: str,
        evaluator_identity: str,
    ) -> None:
        if partition == "holdout" and evaluator_identity == "author":
            raise DatasetError("holdout requires independent evaluator identity")
        key = (candidate_id, manifest.manifest_sha256, partition)
        self._counts[key] = self._counts.get(key, 0) + 1

    def count(self, candidate_id: str, manifest_hash: str, partition: str) -> int:
        return self._counts.get((candidate_id, manifest_hash, partition), 0)
