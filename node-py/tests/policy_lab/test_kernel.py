from livestack_node.policy_lab.kernel import EventKernel


def test_s28_equal_timestamp_deadline_completion_and_cleanup_are_deterministic():
    kernel = EventKernel(horizon_us=100, drain_horizon_us=100, max_events=20, max_same_time=20)
    kernel.schedule(10, 0, "deadline_fence", "request")
    kernel.schedule(10, 1, "terminal_completion", "request", {"result": "ok"})
    kernel.schedule(10, 1, "cleanup", "request")
    kernel.schedule(10, 2, "arrival", "next")
    result = kernel.run()
    assert [row.kind for row in result.processed] == [
        "terminal_completion",
        "cleanup",
        "arrival",
    ]
    assert result.skipped_deadline_fences == 1


def test_stale_events_are_invalidated_by_generation():
    kernel = EventKernel(horizon_us=100, drain_horizon_us=100, max_events=20, max_same_time=20)
    old = kernel.generation("transfer")
    kernel.schedule(10, 1, "completion", "transfer", generation=old)
    kernel.bump_generation("transfer")
    kernel.schedule(20, 1, "completion", "transfer", generation=kernel.generation("transfer"))
    result = kernel.run()
    assert [row.time_us for row in result.processed] == [20]
    assert result.stale_events == 1


def test_s30_dictionary_order_does_not_change_semantic_digest():
    def replay(items):
        kernel = EventKernel(horizon_us=100, drain_horizon_us=100, max_events=20, max_same_time=20)
        kernel.schedule_many(
            [
                {"time_us": 1, "phase": 2, "kind": "arrival", "entity_id": key, "payload": value}
                for key, value in items
            ]
        )
        return kernel.run().semantic_sha256

    values = {"b": {"work": 2}, "a": {"work": 1}, "c": {"work": 3}}
    assert replay(values.items()) == replay(reversed(list(values.items())))


def test_s31_sustained_zero_time_overload_terminates_at_same_time_bound():
    kernel = EventKernel(horizon_us=100, drain_horizon_us=100, max_events=100, max_same_time=5)

    def loop(event, runtime):
        runtime.schedule(event.time_us, 3, "loop", event.entity_id)

    kernel.schedule(0, 3, "loop", "overload")
    result = kernel.run({"loop": loop})
    assert result.termination == "max_same_time"
    assert len(result.processed) == 5
    assert result.unfinished_events == 1


def test_manually_calculable_single_server_queue():
    kernel = EventKernel(horizon_us=100, drain_horizon_us=100, max_events=20, max_same_time=20)
    free_at = 0
    completions = {}

    def arrival(event, runtime):
        nonlocal free_at
        start = max(event.time_us, free_at)
        free_at = start + event.payload["service_us"]
        completions[event.entity_id] = free_at
        runtime.schedule(free_at, 1, "completion", event.entity_id)

    kernel.schedule_many(
        [
            {"time_us": 0, "phase": 2, "kind": "arrival", "entity_id": "a", "payload": {"service_us": 5}},
            {"time_us": 2, "phase": 2, "kind": "arrival", "entity_id": "b", "payload": {"service_us": 3}},
        ]
    )
    kernel.run({"arrival": arrival})
    assert completions == {"a": 5, "b": 8}
