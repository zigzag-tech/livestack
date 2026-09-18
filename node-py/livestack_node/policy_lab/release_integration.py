"""Read-only shadow and isolated future-release contract fixtures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import ContractError


@dataclass(frozen=True)
class ReleaseBinding:
    policy_sha256: str
    config_sha256: str
    profile_sha256: str
    domain_sha256: str
    evaluator_sha256: str

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ContractError(f"release {field} must be a SHA-256 digest")


@dataclass(frozen=True)
class AttemptOwnership:
    request_id: str
    policy_sha256: str
    release_epoch: int
    fence: int


class ShadowAdapter:
    """Policy access to a copy of observations with no executor capability."""

    def __init__(self, policy: Callable[[Mapping[str, Any]], Any]) -> None:
        self._policy = policy

    def recommend(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        recommendation = self._policy(dict(observation))
        return {
            "recommendation": recommendation,
            "reservations": 0,
            "model_loads": 0,
            "route_changes": 0,
            "shadow_only": True,
        }


class ReleaseController:
    """Isolated release-state fixture; not connected to a live router."""

    def __init__(self, incumbent: ReleaseBinding) -> None:
        self.current = incumbent
        self.epoch = 1
        self._fence = 0
        self._attempts: dict[str, AttemptOwnership] = {}
        self._committed: set[str] = set()

    def activate(
        self,
        candidate: ReleaseBinding,
        *,
        evidence_level: str,
        authorization: Mapping[str, Any] | None,
    ) -> None:
        if authorization is None:
            raise ContractError("separate release authorization is required")
        if evidence_level not in {"canary", "active"}:
            raise ContractError("offline qualification cannot activate without canary evidence")
        if (
            not authorization.get("authorization_id")
            or not authorization.get("authorized_by")
            or authorization.get("binding") != candidate
        ):
            raise ContractError("release authorization is not bound to exact artifacts")
        self.epoch += 1
        self.current = candidate

    def rollback(self, rollback_target: ReleaseBinding) -> None:
        self.epoch += 1
        self.current = rollback_target

    def start(self, request_id: str) -> AttemptOwnership:
        if not request_id or request_id in self._attempts:
            raise ContractError("request already has attempt ownership")
        self._fence += 1
        attempt = AttemptOwnership(request_id, self.current.policy_sha256, self.epoch, self._fence)
        self._attempts[request_id] = attempt
        return attempt

    def commit(self, request_id: str, fence: int) -> str:
        attempt = self._attempts.get(request_id)
        if attempt is None or attempt.fence != fence:
            raise ContractError("attempt fence does not own commit")
        if request_id in self._committed:
            return "duplicate"
        self._committed.add(request_id)
        return "committed"
