from livestack_node import fleet_scheduler as harmony
from livestack_node.policy_lab.incumbent import CurrentHarmonyAdapter


def test_current_harmony_adapter_has_pinned_source_and_action_parity():
    state = harmony.FleetState(
        targets=(
            harmony.Target("near", "host-a", harmony.Tier.LOCAL, {"slot": 1}, distance_ms=10),
            harmony.Target("far", "host-b", harmony.Tier.LOCAL, {"slot": 1}, distance_ms=100),
        ),
        jobs=(harmony.Job("job", "llm", sla=harmony.Sla.INTERACTIVE),),
    )
    direct = harmony.schedule(state)
    adapter = CurrentHarmonyAdapter()
    adapted = adapter.decide(state)
    assert adapted.actions == direct.actions
    assert adapter.source_sha256 == "b5b1b9b1b3c7d9e6e6b76928d6c0a44f2064297d6ceaa3dfcf38f744772c972f"
    assert "deep_queue_wait" in adapter.unmapped_semantics
    assert "cold_not_ready_target" in adapter.unmapped_semantics
