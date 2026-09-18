import pytest

from livestack_node.policy_lab.policy_contract import (
    ActionProposal,
    CandidateState,
    PolicyInput,
    PolicyValidationError,
    ProposalValidator,
)


def _input():
    return PolicyInput(
        request_id="request",
        attempt_id="attempt",
        now_us=10,
        deadline_us=100,
        capability="tts",
        permitted_regions=("canada",),
        candidates=(
            CandidateState(
                worker_id="worker",
                model_revision="model",
                route_id="route",
                region_id="canada",
                capabilities=("tts",),
                available_resources=(("gpu_bytes", 100),),
                replica_state="resident",
                active_leases=0,
                loadable=True,
            ),
        ),
    )


def test_valid_dispatch_is_accepted_without_mutating_proposal():
    proposal = ActionProposal(
        kind="dispatch",
        request_id="request",
        attempt_id="attempt",
        worker_id="worker",
        model_revision="model",
        route_id="route",
        resources=(("gpu_bytes", 50),),
    )
    assert ProposalValidator(max_actions=2).validate(_input(), (proposal,)) == (proposal,)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"worker_id": "unknown"}, "unknown candidate"),
        ({"route_id": "other"}, "unknown candidate"),
        ({"resources": (("gpu_bytes", 101),)}, "capacity"),
        ({"model_revision": "wrong"}, "unknown candidate"),
    ],
)
def test_illegal_dispatch_fails_instead_of_being_repaired(change, message):
    values = dict(
        kind="dispatch", request_id="request", attempt_id="attempt", worker_id="worker",
        model_revision="model", route_id="route", resources=(("gpu_bytes", 50),)
    )
    values.update(change)
    with pytest.raises(PolicyValidationError, match=message):
        ProposalValidator(max_actions=2).validate(_input(), (ActionProposal(**values),))


def test_active_replica_eviction_and_unbounded_actions_are_invalid():
    active = _input().candidates[0]
    changed = CandidateState(**{**active.__dict__, "active_leases": 1})
    policy_input = PolicyInput(**{**_input().__dict__, "candidates": (changed,)})
    eviction = ActionProposal(kind="evict_idle_replica", worker_id="worker", model_revision="model")
    with pytest.raises(PolicyValidationError, match="active lease"):
        ProposalValidator(max_actions=2).validate(policy_input, (eviction,))
    with pytest.raises(PolicyValidationError, match="action budget"):
        ProposalValidator(max_actions=1).validate(_input(), (eviction, eviction))
