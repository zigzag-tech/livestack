"""Counterfactual workflow DAG and digest artifact availability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import ContractError


class WorkflowError(ContractError):
    pass


@dataclass(frozen=True)
class WorkflowNode:
    request_id: str
    dependencies: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    produced_artifacts: tuple[str, ...]
    think_time_us: int

    def __post_init__(self) -> None:
        if not self.request_id or type(self.think_time_us) is not int or self.think_time_us < 0:
            raise WorkflowError("invalid workflow node")


@dataclass(frozen=True)
class ArtifactAvailability:
    artifact_id: str
    region_id: str
    available_at_us: int
    producer_request_id: str


class WorkflowGraph:
    def __init__(self, nodes: list[WorkflowNode]) -> None:
        self.nodes = {node.request_id: node for node in nodes}
        if len(self.nodes) != len(nodes):
            raise WorkflowError("duplicate workflow request_id")
        for node in nodes:
            for dependency in node.dependencies:
                if dependency not in self.nodes:
                    raise WorkflowError(f"missing dependency {dependency} for {node.request_id}")
        self._validate_acyclic()
        self.completions: dict[str, int] = {}
        self.artifacts: dict[str, ArtifactAvailability] = {}

    def _validate_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(request_id: str) -> None:
            if request_id in visiting:
                raise WorkflowError("workflow cycle detected")
            if request_id in visited:
                return
            visiting.add(request_id)
            for dependency in self.nodes[request_id].dependencies:
                visit(dependency)
            visiting.remove(request_id)
            visited.add(request_id)

        for request_id in sorted(self.nodes):
            visit(request_id)

    def complete(
        self,
        request_id: str,
        *,
        at_us: int,
        artifacts: Mapping[str, tuple[str, int]],
    ) -> None:
        node = self.nodes[request_id]
        if request_id in self.completions:
            raise WorkflowError(f"request already completed: {request_id}")
        if type(at_us) is not int or at_us < 0:
            raise WorkflowError("completion time must be nonnegative")
        if set(artifacts) - set(node.produced_artifacts):
            raise WorkflowError("completion published undeclared artifact")
        self.completions[request_id] = at_us
        for artifact_id, (region_id, available_at_us) in artifacts.items():
            if not region_id or type(available_at_us) is not int or available_at_us < at_us:
                raise WorkflowError("invalid artifact availability")
            self.artifacts[artifact_id] = ArtifactAvailability(
                artifact_id, region_id, available_at_us, request_id
            )

    def runnable_at(self, request_id: str) -> int | None:
        node = self.nodes[request_id]
        if not node.dependencies:
            return 0
        if any(dependency not in self.completions for dependency in node.dependencies):
            return None
        if any(artifact not in self.artifacts for artifact in node.required_artifacts):
            return None
        boundary = max(self.completions[dependency] for dependency in node.dependencies)
        if node.required_artifacts:
            boundary = max(
                boundary,
                *(self.artifacts[artifact].available_at_us for artifact in node.required_artifacts),
            )
        return boundary + node.think_time_us
