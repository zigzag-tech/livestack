"""Versioned S01-S32 scenario fixture catalog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError, load_json


CATALOG_PATH = Path(__file__).with_name("fixtures") / "scenarios-v1.json"


@dataclass(frozen=True)
class ScenarioFixture:
    id: str
    topology: str
    arrivals: str
    exogenous: str
    profile_pack: str
    oracle_assertions: tuple[str, ...]
    reason: str
    test_file: str


def load_scenarios() -> tuple[ScenarioFixture, ...]:
    value = load_json(CATALOG_PATH.read_bytes(), max_bytes=256 * 1024)
    if set(value) != {"schema_version", "kind", "scenarios"} or value["schema_version"] != 1 or value["kind"] != "scenario_catalog":
        raise ContractError("invalid scenario catalog envelope")
    scenarios = tuple(
        ScenarioFixture(
            id=item["id"],
            topology=item["topology"],
            arrivals=item["arrivals"],
            exogenous=item["exogenous"],
            profile_pack=item["profile_pack"],
            oracle_assertions=tuple(item["oracle_assertions"]),
            reason=item["reason"],
            test_file=item["test_file"],
        )
        for item in value["scenarios"]
    )
    expected = {f"S{index:02d}" for index in range(1, 33)}
    if {scenario.id for scenario in scenarios} != expected or len(scenarios) != 32:
        raise ContractError("scenario catalog must contain S01-S32 exactly once")
    return scenarios
