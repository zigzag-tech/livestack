import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.scenario_admission import ScenarioRegistry


def test_independent_curator_admits_once_and_repeated_incidents_only_change_weight():
    registry = ScenarioRegistry()
    proposal = {
        "mechanism_id": "wan-drop-during-tts",
        "topology": "two regions",
        "assertions": ["bytes conserved", "underrun reported"],
        "incident_sha256": "a" * 64,
    }
    first = registry.admit(proposal, author_identity="author", curator_identity="curator")
    repeated = registry.admit(
        {**proposal, "incident_sha256": "b" * 64},
        author_identity="author",
        curator_identity="curator",
    )
    assert first["scenario_id"] == repeated["scenario_id"]
    assert repeated["oracle_count"] == 1
    assert repeated["incident_count"] == 2
    assert repeated["traffic_weight"] == 2


def test_author_cannot_self_admit_or_change_oracle_for_same_mechanism():
    registry = ScenarioRegistry()
    proposal = {
        "mechanism_id": "m",
        "topology": "t",
        "assertions": ["a"],
        "incident_sha256": "a" * 64,
    }
    with pytest.raises(ContractError, match="independent"):
        registry.admit(proposal, author_identity="same", curator_identity="same")
    registry.admit(proposal, author_identity="author", curator_identity="curator")
    with pytest.raises(ContractError, match="oracle"):
        registry.admit(
            {**proposal, "assertions": ["different"], "incident_sha256": "b" * 64},
            author_identity="author",
            curator_identity="curator",
        )
