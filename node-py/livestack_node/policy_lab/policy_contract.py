"""Pure policy input/proposals and independent hard-action validation."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError


class PolicyValidationError(ContractError):
    pass


ALLOWED_ACTIONS = frozenset(
    {"dispatch", "queue_until", "refuse", "prepare_replica", "retain_until", "evict_idle_replica"}
)


@dataclass(frozen=True)
class CandidateState:
    worker_id: str
    model_revision: str
    route_id: str
    region_id: str
    capabilities: tuple[str, ...]
    available_resources: tuple[tuple[str, int], ...]
    replica_state: str
    active_leases: int
    loadable: bool


@dataclass(frozen=True)
class PolicyInput:
    request_id: str
    attempt_id: str
    now_us: int
    deadline_us: int | None
    capability: str
    permitted_regions: tuple[str, ...]
    candidates: tuple[CandidateState, ...]


@dataclass(frozen=True)
class ActionProposal:
    kind: str
    request_id: str | None = None
    attempt_id: str | None = None
    worker_id: str | None = None
    model_revision: str | None = None
    route_id: str | None = None
    resources: tuple[tuple[str, int], ...] = ()
    until_us: int | None = None
    reason_code: str | None = None


class ProposalValidator:
    def __init__(self, *, max_actions: int) -> None:
        if max_actions <= 0:
            raise ValueError("max_actions must be positive")
        self.max_actions = max_actions

    def validate(
        self, policy_input: PolicyInput, proposals: tuple[ActionProposal, ...]
    ) -> tuple[ActionProposal, ...]:
        if len(proposals) > self.max_actions:
            raise PolicyValidationError("policy action budget exceeded")
        for proposal in proposals:
            if proposal.kind not in ALLOWED_ACTIONS:
                raise PolicyValidationError(f"unsupported action kind: {proposal.kind}")
            if proposal.kind == "dispatch":
                self._dispatch(policy_input, proposal)
            elif proposal.kind == "prepare_replica":
                candidate = self._candidate(policy_input, proposal)
                if candidate.replica_state not in {"absent", "loading"} or not candidate.loadable:
                    raise PolicyValidationError("replica cannot be prepared")
            elif proposal.kind == "evict_idle_replica":
                candidate = self._candidate(policy_input, proposal, ignore_route=True)
                if candidate.replica_state != "resident" or candidate.active_leases:
                    raise PolicyValidationError("eviction violates active lease or state")
            elif proposal.kind in {"queue_until", "retain_until"}:
                if proposal.until_us is None or proposal.until_us < policy_input.now_us:
                    raise PolicyValidationError("action until_us is invalid")
            elif proposal.kind == "refuse" and not proposal.reason_code:
                raise PolicyValidationError("refusal requires reason_code")
        return proposals

    @staticmethod
    def _candidate(
        policy_input: PolicyInput,
        proposal: ActionProposal,
        *,
        ignore_route: bool = False,
    ) -> CandidateState:
        matches = [
            candidate
            for candidate in policy_input.candidates
            if candidate.worker_id == proposal.worker_id
            and candidate.model_revision == proposal.model_revision
            and (ignore_route or candidate.route_id == proposal.route_id)
        ]
        if len(matches) != 1:
            raise PolicyValidationError("proposal references unknown candidate")
        return matches[0]

    def _dispatch(self, policy_input: PolicyInput, proposal: ActionProposal) -> None:
        if proposal.request_id != policy_input.request_id or proposal.attempt_id != policy_input.attempt_id:
            raise PolicyValidationError("dispatch identity mismatch")
        candidate = self._candidate(policy_input, proposal)
        if candidate.region_id not in policy_input.permitted_regions:
            raise PolicyValidationError("dispatch violates region permission")
        if policy_input.capability not in candidate.capabilities or candidate.replica_state != "resident":
            raise PolicyValidationError("dispatch violates capability or residency")
        if policy_input.deadline_us is not None and policy_input.now_us >= policy_input.deadline_us:
            raise PolicyValidationError("dispatch begins at or after deadline")
        available = dict(candidate.available_resources)
        requested = dict(proposal.resources)
        if len(requested) != len(proposal.resources) or any(
            name not in available or type(value) is not int or value < 0 or value > available[name]
            for name, value in proposal.resources
        ):
            raise PolicyValidationError("dispatch exceeds capacity")
