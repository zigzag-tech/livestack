"""Pinned adapter for the existing pure Harmony fleet scheduler."""

from __future__ import annotations

from dataclasses import dataclass

from livestack_node import fleet_scheduler


@dataclass(frozen=True)
class IncumbentDecision:
    actions: tuple[fleet_scheduler.FleetAction, ...]
    source_sha256: str
    unmapped_semantics: tuple[str, ...]


class CurrentHarmonyAdapter:
    source_sha256 = "b5b1b9b1b3c7d9e6e6b76928d6c0a44f2064297d6ceaa3dfcf38f744772c972f"
    unmapped_semantics = (
        "deep_queue_wait",
        "cold_not_ready_target",
        "streaming_playback_state",
        "model_load_failure",
        "regional_payload_path",
    )

    def decide(
        self,
        state: fleet_scheduler.FleetState,
        policy: fleet_scheduler.SchedulerPolicy | None = None,
    ) -> IncumbentDecision:
        plan = fleet_scheduler.schedule(state, policy)
        return IncumbentDecision(plan.actions, self.source_sha256, self.unmapped_semantics)
