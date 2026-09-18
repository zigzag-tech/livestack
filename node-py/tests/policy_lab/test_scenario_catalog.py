from pathlib import Path

from livestack_node.policy_lab.scenarios import load_scenarios


def test_s01_s32_have_complete_fixture_inputs_and_independent_oracles():
    tests_root = Path(__file__).parent
    scenarios = load_scenarios()
    assert len(scenarios) == 32
    for scenario in scenarios:
        assert scenario.topology
        assert scenario.arrivals
        assert scenario.exogenous
        assert scenario.profile_pack
        assert scenario.reason
        assert scenario.oracle_assertions
        assert all("snapshot chosen host" not in assertion for assertion in scenario.oracle_assertions)
        assert (tests_root / scenario.test_file).exists(), scenario.id
