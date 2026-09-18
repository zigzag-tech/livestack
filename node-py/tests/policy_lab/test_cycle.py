import hashlib

import pytest

from livestack_node.policy_lab.cycle import (
    CycleMachine,
    coalesce_trigger,
    plan_cycle,
    submit_cycle,
    run_isolated_cycle,
)
from livestack_node.policy_lab.contracts import ContractError
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore


def _config(**changes):
    value = {
        "schema_version": 1,
        "kind": "cycle_config",
        "domain_id": "routing-v1",
        "data_cutoff_utc_us": 10_000,
        "source_sha256": "a" * 64,
        "profile_sha256": "b" * 64,
        "dataset_sha256": "c" * 64,
        "evaluator_sha256": "d" * 64,
        "candidate_limit": 4,
        "revisions_per_candidate": 2,
        "evaluation_limit": 8,
        "wall_seconds": 7200,
        "cpu": 4,
        "memory_bytes": 8 * 1024**3,
        "output_bytes": 1024**3,
        "gpu_seconds": 0,
        "authoring_token_budget": None,
        "authoring_spend_microusd": None,
        "weekly_trigger_enabled": False,
    }
    value.update(changes)
    return value


def test_cycle_plan_is_report_only_without_finite_author_budget_and_never_submits():
    plan = plan_cycle(_config())
    assert plan["mode"] == "report_only"
    assert plan["state"] == "planned"
    assert plan["submission_count"] == 0
    assert plan["scheduler_installed"] is False
    assert plan["gpu_seconds"] == 0


def test_cycle_submit_reuses_durable_authority_idempotency_and_conflicts_cleanly(tmp_path):
    store = WorkloadStore(tmp_path / "workloads.sqlite", handlers={"policy_lab_cycle"})

    class Authority:
        def submit(self, request):
            return store.submit("policy-lab", request)

    plan = plan_cycle(_config())
    first = submit_cycle(plan, Authority(), handler="policy_lab_cycle", input_digest="e" * 64)
    # Reopen the durable authority to model submitter/authority process restart.
    store = WorkloadStore(tmp_path / "workloads.sqlite", handlers={"policy_lab_cycle"})
    second = submit_cycle(plan, Authority(), handler="policy_lab_cycle", input_digest="e" * 64)
    assert first["job_id"] == second["job_id"]

    changed = dict(plan)
    changed["dataset_sha256"] = "f" * 64
    with pytest.raises(WorkloadError, match="idempotency key"):
        submit_cycle(changed, Authority(), handler="policy_lab_cycle", input_digest="e" * 64)


def test_cycle_state_machine_is_finite_resumable_and_keeps_incumbent_on_budget_exhaustion():
    machine = CycleMachine("cycle", candidate_limit=1, revision_limit=1, evaluation_limit=1)
    for state in ("admitted", "gathering", "frozen", "authoring"):
        assert machine.advance(state) == state
    machine.record_candidate("candidate-a", "hypothesis", "tradeoff")
    machine.record_revision("candidate-a")
    assert machine.advance("evaluating") == "evaluating"
    machine.record_evaluation("candidate-a", status="no_change")
    assert machine.hidden_evaluation_exposures == 1
    with pytest.raises(ContractError, match="exhausted"):
        machine.record_evaluation("candidate-a", status="offline_qualified")
    assert machine.advance("reported") == "reported"
    assert machine.advance("no_change") == "no_change"
    assert machine.incumbent_changed is False
    with pytest.raises(ContractError, match="terminal"):
        machine.advance("authoring")


def test_active_trigger_coalesces_later_cutoff_without_mutating_active_cycle():
    active = plan_cycle(_config(data_cutoff_utc_us=10_000))
    incoming = plan_cycle(_config(data_cutoff_utc_us=20_000))
    result = coalesce_trigger(active, incoming)
    assert result["active_cycle_id"] == active["cycle_id"]
    assert result["next_data_cutoff_utc_us"] == 20_000
    assert active["data_cutoff_utc_us"] == 10_000


def test_isolated_cycle_is_reproducible_rejects_invalid_candidate_and_keeps_incumbent():
    first = run_isolated_cycle(seed=7)
    second = run_isolated_cycle(seed=7)
    assert first == second
    assert first["candidates"]["useful"]["status"] == "evaluated_no_change"
    assert first["candidates"]["invalid"]["status"] == "rejected"
    assert first["outcome"] == "no_change"
    assert first["incumbent_changed"] is False
    assert len(first["release_evidence_sha256"]) == 64
