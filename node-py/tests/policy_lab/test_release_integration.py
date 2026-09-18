import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.release_integration import (
    ReleaseBinding,
    ReleaseController,
    ShadowAdapter,
)


def _binding(policy="a"):
    return ReleaseBinding(
        policy_sha256=policy * 64,
        config_sha256="b" * 64,
        profile_sha256="c" * 64,
        domain_sha256="d" * 64,
        evaluator_sha256="e" * 64,
    )


def test_shadow_recommendation_has_no_reservation_load_or_route_side_effects():
    shadow = ShadowAdapter(lambda observation: {"worker_id": observation["candidate"]})
    result = shadow.recommend({"candidate": "worker-a"})
    assert result["recommendation"] == {"worker_id": "worker-a"}
    assert result["reservations"] == 0
    assert result["model_loads"] == 0
    assert result["route_changes"] == 0


def test_offline_qualification_without_separate_authorization_cannot_activate():
    controller = ReleaseController(_binding("a"))
    with pytest.raises(ContractError, match="authorization"):
        controller.activate(_binding("f"), evidence_level="offline_qualified", authorization=None)
    with pytest.raises(ContractError, match="canary"):
        controller.activate(
            _binding("f"), evidence_level="offline_qualified",
            authorization={"authorization_id": "auth", "authorized_by": "operator", "binding": _binding("f")},
        )


def test_rollback_switches_new_work_but_preserves_and_fences_ongoing_attempts():
    incumbent = _binding("a")
    candidate = _binding("f")
    controller = ReleaseController(incumbent)
    old = controller.start("old-stream")
    controller.activate(
        candidate,
        evidence_level="canary",
        authorization={"authorization_id": "auth", "authorized_by": "operator", "binding": candidate},
    )
    candidate_attempt = controller.start("candidate-stream")
    controller.rollback(incumbent)
    new = controller.start("new-stream")
    assert old.policy_sha256 == incumbent.policy_sha256
    assert candidate_attempt.policy_sha256 == candidate.policy_sha256
    assert new.policy_sha256 == incumbent.policy_sha256
    assert controller.commit("candidate-stream", candidate_attempt.fence) == "committed"
    assert controller.commit("candidate-stream", candidate_attempt.fence) == "duplicate"
    with pytest.raises(ContractError, match="fence"):
        controller.commit("old-stream", old.fence + 1)
