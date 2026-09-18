from livestack_node.policy_lab.policies import (
    RoutingCandidate,
    RoutingRequest,
    least_queue,
    nearest_ready,
    residency_proposals,
    total_latency_demand_aware,
    warm_first,
)


def _candidate(worker, **changes):
    values = dict(
        worker_id=worker,
        model_revision="model",
        route_id=f"route-{worker}",
        region_id=worker,
        eligible=True,
        ready=True,
        loadable=True,
        distance_us=10,
        queue_us=0,
        preparation_us=0,
        transfer_us=0,
        execution_us=100,
        uncertainty_us=0,
        observation_age_us=0,
        demand_count=1.0,
        demand_observed_at_us=0,
    )
    values.update(changes)
    return RoutingCandidate(**values)


def _request(**changes):
    values = dict(
        request_id="request",
        attempt_id="attempt",
        now_us=100,
        deadline_us=10_000,
        wait_age_us=0,
        fairness_bound_us=1_000,
    )
    values.update(changes)
    return RoutingRequest(**values)


def test_s01_nearest_ready_and_warm_first_choose_lower_latency_ready_route():
    candidates = (_candidate("local", distance_us=10), _candidate("remote", distance_us=100))
    assert nearest_ready(_request(), candidates).chosen_worker_id == "local"
    assert warm_first(_request(), candidates).chosen_worker_id == "local"


def test_s02_least_queue_can_choose_idle_remote_over_busy_local():
    candidates = (
        _candidate("local", distance_us=10, queue_us=1_000),
        _candidate("remote", distance_us=100, queue_us=0, transfer_us=50),
    )
    decision = least_queue(_request(), candidates)
    assert decision.chosen_worker_id == "remote"
    remote = next(trace for trace in decision.candidates if trace.worker_id == "remote")
    assert remote.components["transfer_us"] == 50


def test_s03_cold_preparation_is_charged_under_shared_cost_surface():
    cold = _candidate("local", ready=False, preparation_us=1_000)
    warm = _candidate("remote", distance_us=100)
    decision = total_latency_demand_aware(_request(), (cold, warm))
    assert decision.chosen_worker_id == "remote"
    local_trace = next(trace for trace in decision.candidates if trace.worker_id == "local")
    assert local_trace.total_upper_us == 1_110


def test_s04_current_remote_dispatch_and_future_local_prepare_are_separate_actions():
    cold_local = _candidate("local", ready=False, preparation_us=500, demand_count=20)
    warm_remote = _candidate("remote", distance_us=50, demand_count=1)
    decision = total_latency_demand_aware(
        _request(), (cold_local, warm_remote), prepare_demand_threshold=5
    )
    assert decision.chosen_worker_id == "remote"
    assert [(action.kind, action.worker_id) for action in decision.actions] == [
        ("dispatch", "remote"),
        ("prepare_replica", "local"),
    ]


def test_s05_demand_decay_removes_old_pin_and_rare_fairness_still_dispatches():
    old_popular = _candidate("old", demand_count=100, demand_observed_at_us=0)
    recent = _candidate("recent", demand_count=2, demand_observed_at_us=9_900)
    retained = residency_proposals((old_popular, recent), now_us=10_000, half_life_us=100, slots=1)
    assert retained[0].worker_id == "recent"
    fair = total_latency_demand_aware(
        _request(wait_age_us=2_000, fairness_bound_us=1_000),
        (_candidate("rare", demand_count=0),),
    )
    assert fair.chosen_worker_id == "rare"
    assert fair.reason_code == "fairness_bound"


def test_s06_capacity_for_two_incompatible_models_retains_both_without_churn():
    a = _candidate("worker-a", model_revision="model-a", demand_count=5)
    b = _candidate("worker-b", model_revision="model-b", demand_count=5)
    retained = residency_proposals((a, b), now_us=100, half_life_us=1_000, slots=2)
    assert {proposal.model_revision for proposal in retained} == {"model-a", "model-b"}
