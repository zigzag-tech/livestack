import json
from pathlib import Path

from livestack_node.policy_lab.analytic import (
    execution_duration_us,
    load_duration_us,
    peak_memory_bytes,
    transfer_duration_us,
)


FIXTURE = Path(__file__).parents[2] / "livestack_node" / "policy_lab" / "fixtures" / "analytic-profiles-v1.json"


def test_tiny_profiles_have_manually_calculable_durations_and_memory():
    profile = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert load_duration_us(profile["load"]) == 2_100_000
    assert transfer_duration_us(profile["transfer"]) == 2_050_000
    assert execution_duration_us(profile["execution"]) == 2_000_000
    assert peak_memory_bytes(profile["memory"]) == 1_500
