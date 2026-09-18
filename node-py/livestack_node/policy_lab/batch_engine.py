"""Generic bounded batch stage with checkpoint-only interruption."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError


class BatchError(ContractError):
    pass


@dataclass
class BatchStage:
    stage_id: str
    total_work_units: int
    checkpoints: tuple[int, ...]
    resource_vector: dict[str, int]
    output_artifacts: tuple[str, ...]
    eligible_at_us: int

    def __post_init__(self) -> None:
        if (
            not self.stage_id
            or self.total_work_units <= 0
            or self.eligible_at_us < 0
            or any(point <= 0 or point >= self.total_work_units for point in self.checkpoints)
            or tuple(sorted(set(self.checkpoints))) != self.checkpoints
        ):
            raise BatchError("invalid batch stage")
        if not self.resource_vector or any(value < 0 for value in self.resource_vector.values()):
            raise BatchError("invalid batch resource vector")
        self.state = "eligible"
        self.completed_work_units = 0
        self.started_at_us: int | None = None
        self.failure_class: str | None = None

    @property
    def wait_for_eligibility_us(self) -> int | None:
        if self.started_at_us is None:
            return None
        return self.started_at_us - self.eligible_at_us

    def starvation_age_us(self, *, at_us: int) -> int:
        if at_us < self.eligible_at_us:
            return 0
        if self.started_at_us is not None:
            return self.started_at_us - self.eligible_at_us
        return at_us - self.eligible_at_us

    def start(self, *, at_us: int) -> None:
        if self.state not in {"eligible", "interrupted"} or at_us < self.eligible_at_us:
            raise BatchError("batch stage cannot start")
        if self.started_at_us is None:
            self.started_at_us = at_us
        self.state = "running"

    def advance_to(self, work_units: int) -> None:
        if self.state != "running" or not self.completed_work_units <= work_units <= self.total_work_units:
            raise BatchError("invalid batch progress")
        self.completed_work_units = work_units

    def interrupt(self) -> None:
        if self.state != "running" or self.completed_work_units not in self.checkpoints:
            raise BatchError("batch interruption is legal only at a checkpoint")
        self.state = "interrupted"

    def resume(self) -> None:
        if self.state != "interrupted":
            raise BatchError("batch stage is not interrupted")
        self.state = "running"

    def complete(self, *, at_us: int) -> tuple[str, ...]:
        if self.state != "running" or self.completed_work_units != self.total_work_units:
            raise BatchError("batch stage is not complete")
        self.state = "completed"
        return self.output_artifacts

    def fail(self, failure_class: str) -> None:
        if self.state not in {"running", "eligible"}:
            raise BatchError("batch stage cannot fail from current state")
        self.state = "failed"
        self.failure_class = failure_class

    def scheduler_retry(self) -> None:
        if self.failure_class == "semantic_validation":
            raise BatchError("semantic repair must be a new application child request")
        raise BatchError("scheduler retries require an explicit scenario transition")
