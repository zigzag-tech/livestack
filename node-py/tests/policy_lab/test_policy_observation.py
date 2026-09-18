from dataclasses import FrozenInstanceError

import pytest

from livestack_node.policy_lab.policy_observation import ObservationBus, TruthRequest


def test_s17_observations_are_delayed_and_truth_is_not_exposed():
    bus = ObservationBus(default_lag_us=50)
    bus.add_truth_request(
        TruthRequest(
            request_id="future",
            arrival_us=100,
            estimated_output_units=10,
            actual_output_units=999,
        )
    )
    bus.publish_worker(
        observed_at_us=10,
        worker_id="worker",
        state={"ready": True, "queue_depth": 1},
    )
    bus.set_hidden_worker_state("worker", {"ready": False, "queue_depth": 99})

    early = bus.observe(59)
    assert early.workers == ()
    assert early.requests == ()

    visible_worker = bus.observe(60)
    assert visible_worker.workers[0].state["ready"] is True
    assert visible_worker.workers[0].state["queue_depth"] == 1

    before_arrival_delivery = bus.observe(149)
    assert before_arrival_delivery.requests == ()
    after_arrival_delivery = bus.observe(150)
    assert after_arrival_delivery.requests[0].estimated_output_units == 10
    assert not hasattr(after_arrival_delivery.requests[0], "actual_output_units")


def test_s26_policy_observation_is_deeply_immutable():
    bus = ObservationBus(default_lag_us=0)
    bus.publish_worker(
        observed_at_us=0,
        worker_id="worker",
        state={"ready": True, "nested": {"capacity": 1}},
    )
    observation = bus.observe(0)
    with pytest.raises(TypeError):
        observation.workers[0].state["ready"] = False
    with pytest.raises(TypeError):
        observation.workers[0].state["nested"]["capacity"] = 2
    with pytest.raises(FrozenInstanceError):
        observation.cutoff_us = 1
