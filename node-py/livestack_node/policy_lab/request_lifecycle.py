"""Strict logical-request and physical-attempt state transitions."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError


class LifecycleError(ContractError):
    pass


@dataclass
class AttemptState:
    attempt_id: str
    state: str
    started_at_us: int
    lease_id: str | None = None
    failure_class: str | None = None
    resource_released: bool = False


class RequestLifecycle:
    def __init__(self, request_id: str, *, arrival_us: int, deadline_us: int | None) -> None:
        if not request_id or type(arrival_us) is not int or arrival_us < 0:
            raise LifecycleError("invalid request identity or arrival")
        if deadline_us is not None and (type(deadline_us) is not int or deadline_us < arrival_us):
            raise LifecycleError("deadline must be null or no earlier than arrival")
        self.request_id = request_id
        self.arrival_us = arrival_us
        self.deadline_us = deadline_us
        self.state = "arrived"
        self.attempts: dict[str, AttemptState] = {}
        self.terminal_result_id: str | None = None
        self.completed_at_us: int | None = None
        self.cancellation_cause: str | None = None
        self.resource_release_count = 0

    def _before_deadline(self, now_us: int) -> None:
        if type(now_us) is not int or now_us < self.arrival_us:
            raise LifecycleError("invalid transition time")
        if self.deadline_us is not None and now_us >= self.deadline_us:
            raise LifecycleError("cannot start new work at or after deadline")

    def queue(self, now_us: int) -> None:
        self._before_deadline(now_us)
        if self.state != "arrived":
            raise LifecycleError(f"cannot queue from {self.state}")
        self.state = "queued"

    def start_attempt(self, attempt_id: str, now_us: int) -> None:
        self._before_deadline(now_us)
        if self.state == "terminal" or attempt_id in self.attempts:
            raise LifecycleError("request terminal or duplicate attempt")
        self.attempts[attempt_id] = AttemptState(attempt_id, "queued", now_us)

    def reserve(self, attempt_id: str, now_us: int, lease_id: str) -> None:
        self._before_deadline(now_us)
        attempt = self.attempts[attempt_id]
        if attempt.state != "queued" or not lease_id:
            raise LifecycleError("attempt cannot reserve")
        attempt.state = "reserved"
        attempt.lease_id = lease_id
        self.state = "reserved"

    def mark_preparing(self, attempt_id: str, now_us: int) -> None:
        self._before_deadline(now_us)
        attempt = self.attempts[attempt_id]
        if attempt.state != "reserved":
            raise LifecycleError("attempt cannot prepare")
        attempt.state = "preparing"
        self.state = "preparing"

    def mark_executing(self, attempt_id: str, now_us: int) -> None:
        self._before_deadline(now_us)
        attempt = self.attempts[attempt_id]
        if attempt.state not in {"reserved", "preparing"}:
            raise LifecycleError("attempt cannot execute")
        attempt.state = "executing"
        self.state = "executing"

    def fail_attempt(self, attempt_id: str, now_us: int, failure_class: str) -> None:
        attempt = self.attempts[attempt_id]
        if attempt.state in {"failed", "completed", "canceled"}:
            raise LifecycleError("attempt already terminal")
        attempt.state = "failed"
        attempt.failure_class = failure_class
        if self.state != "terminal":
            self.state = "queued"

    def complete(self, attempt_id: str, now_us: int, result_id: str) -> str:
        attempt = self.attempts[attempt_id]
        if self.terminal_result_id is not None:
            if self.terminal_result_id == result_id and self.completed_at_us == now_us:
                return "duplicate"
            raise LifecycleError("conflicting completion for logical request")
        if attempt.state != "executing":
            raise LifecycleError("only executing attempt can complete")
        if self.deadline_us is not None and now_us > self.deadline_us:
            raise LifecycleError("completion occurred after deadline")
        attempt.state = "completed"
        self.terminal_result_id = result_id
        self.completed_at_us = now_us
        self.state = "terminal"
        return "completed"

    def request_cancel(self, now_us: int, *, cause: str) -> None:
        if self.state == "terminal" or not cause:
            raise LifecycleError("cannot cancel terminal request or omit cause")
        self.cancellation_cause = cause

    def acknowledge_cancel(self, attempt_id: str, now_us: int) -> None:
        if self.cancellation_cause is None:
            raise LifecycleError("cancel was not requested")
        attempt = self.attempts[attempt_id]
        attempt.state = "canceled"
        self.completed_at_us = now_us
        self.state = "terminal"

    def release_after_cleanup(self, attempt_id: str, lease_id: str) -> str:
        attempt = self.attempts[attempt_id]
        if attempt.lease_id != lease_id:
            raise LifecycleError("cleanup lease does not match attempt")
        if attempt.resource_released:
            return "duplicate"
        attempt.resource_released = True
        self.resource_release_count += 1
        return "released"
