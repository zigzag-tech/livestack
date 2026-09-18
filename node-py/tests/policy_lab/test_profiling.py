import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.profiling import plan_profile_matrix, submit_profile_plan
from livestack_node.workloads.model import WorkloadError


def _manifest():
    return {
        "schema_version": 1,
        "kind": "profiling_manifest",
        "domain_id": "two-region-speech-llm-v1",
        "regions": ["region-a", "region-b"],
        "directions": [["region-a", "region-b"], ["region-b", "region-a"]],
        "region_hardware": {"region-a": ["gpu-a"], "region-b": ["gpu-b"]},
        "workloads": [
            {
                "workload_class": "llm-27b",
                "shapes": ["short", "long"],
                "concurrency": [1, 2],
                "cache_states": ["cold", "warm"],
                "model_revisions": ["llm-rev"],
                "runtime_revisions": ["runtime-rev"],
                "minimum_samples_per_cell": 100,
                "minimum_cold_preparations": 20,
            },
            {
                "workload_class": "asr",
                "shapes": ["stream-10s"],
                "concurrency": [1],
                "cache_states": ["warm"],
                "model_revisions": ["asr-rev"],
                "runtime_revisions": ["runtime-rev"],
                "minimum_samples_per_cell": 100,
                "minimum_cold_preparations": 0,
            },
            {
                "workload_class": "tts",
                "shapes": ["sentence"],
                "concurrency": [1],
                "cache_states": ["warm"],
                "model_revisions": ["tts-rev"],
                "runtime_revisions": ["runtime-rev"],
                "minimum_samples_per_cell": 100,
                "minimum_cold_preparations": 0,
            },
        ],
        "observation_windows": 3,
        "resource_budget": {
            "gpu_seconds": 1000,
            "cpu_seconds": 1000,
            "memory_bytes": 1000000000,
            "network_bytes": 1000000000,
        },
        "max_duration_seconds": 3600,
        "protected_service": {
            "minimum_free_gpu_fraction": 0.25,
            "max_added_queue_ms": 100,
            "abort_on_active_stream_interference": True,
        },
    }


def test_profile_plan_expands_two_region_matrix_without_submitting_work():
    plan = plan_profile_matrix(_manifest())
    # Each workload matrix cell is measured in each requester region and window.
    assert plan["cell_count"] == (8 + 1 + 1) * 2 * 3
    assert plan["planned_requests"] == plan["cell_count"] * 100
    assert plan["submission_count"] == 0
    assert plan["execution_status"] == "not_submitted"
    assert plan["authorization_required"] is True
    assert all(cell["sample_target"] >= 100 for cell in plan["cells"])


def test_profile_plan_rejects_missing_region_direction_or_unbounded_budget():
    bad = _manifest()
    bad["directions"] = [["region-a", "region-b"]]
    with pytest.raises(ContractError, match="both regional directions"):
        plan_profile_matrix(bad)

    bad = _manifest()
    bad["resource_budget"]["gpu_seconds"] = None
    with pytest.raises(ContractError, match="resource budget"):
        plan_profile_matrix(bad)


def test_rejected_profile_admission_never_falls_back_to_local_execution():
    class RefusingAuthority:
        def __init__(self):
            self.calls = 0

        def submit(self, request):
            self.calls += 1
            raise WorkloadError("capacity unavailable", 409)

    authority = RefusingAuthority()
    report = submit_profile_plan(
        plan_profile_matrix(_manifest()),
        authority,
        handler="policy_lab_profile",
        input_digest="a" * 64,
        max_jobs=1,
    )
    assert authority.calls == 1
    assert report["status"] == "admission_refused"
    assert report["submitted"] == 0
    assert report["local_fallback_attempts"] == 0


def test_profile_submission_uses_stable_keys_and_bounded_authorized_jobs():
    class Authority:
        def __init__(self):
            self.requests = []

        def submit(self, request):
            self.requests.append(request)
            return {"id": f"job-{len(self.requests)}"}

    authority = Authority()
    report = submit_profile_plan(
        plan_profile_matrix(_manifest()),
        authority,
        handler="policy_lab_profile",
        input_digest="b" * 64,
        max_jobs=2,
    )
    assert report["status"] == "submitted"
    assert report["submitted"] == 2
    assert report["job_ids"] == ["job-1", "job-2"]
    assert authority.requests[0]["key"] != authority.requests[1]["key"]
    assert all(request["need"]["gpu"] > 0 for request in authority.requests)
