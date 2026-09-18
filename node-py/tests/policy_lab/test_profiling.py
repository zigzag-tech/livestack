import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.profiling import plan_profile_matrix, submit_profile_plan
from livestack_node.workloads.model import WorkloadError


def _manifest():
    return {
        "schema_version": 1,
        "kind": "profiling_manifest",
        "domain_id": "two-vantage-speech-llm-v1",
        "requester_vantages": ["vantage-a", "vantage-b"],
        "execution_targets": {
            "worker-a": {"hardware_revisions": ["gpu-a"], "processing_scopes": ["scope-a"]},
            "worker-b": {"hardware_revisions": ["gpu-b"], "processing_scopes": ["scope-b"]},
        },
        "network_paths": [
            {"path_id": "a-to-a", "requester_vantage": "vantage-a", "execution_target": "worker-a"},
            {"path_id": "b-to-a", "requester_vantage": "vantage-b", "execution_target": "worker-a"},
            {"path_id": "b-to-b", "requester_vantage": "vantage-b", "execution_target": "worker-b"},
        ],
        "workloads": [
            {
                "workload_class": "llm-27b",
                "network_path_ids": ["a-to-a", "b-to-a"],
                "target_revisions": {
                    "worker-a": {"model_revisions": ["llm-rev"], "runtime_revisions": ["runtime-a"]}
                },
                "shapes": ["short", "long"],
                "concurrency": [1, 2],
                "cache_states": ["cold", "warm"],
                "minimum_samples_per_cell": 100,
                "minimum_cold_preparations": 20,
            },
            {
                "workload_class": "asr",
                "network_path_ids": ["a-to-a", "b-to-b"],
                "target_revisions": {
                    "worker-a": {"model_revisions": ["asr-rev"], "runtime_revisions": ["runtime-a"]},
                    "worker-b": {"model_revisions": ["asr-rev"], "runtime_revisions": ["runtime-b"]},
                },
                "shapes": ["stream-10s"],
                "concurrency": [1],
                "cache_states": ["warm"],
                "minimum_samples_per_cell": 100,
                "minimum_cold_preparations": 0,
            },
            {
                "workload_class": "tts",
                "network_path_ids": ["a-to-a", "b-to-b"],
                "target_revisions": {
                    "worker-a": {"model_revisions": ["tts-rev"], "runtime_revisions": ["runtime-a"]},
                    "worker-b": {"model_revisions": ["tts-rev"], "runtime_revisions": ["runtime-b"]},
                },
                "shapes": ["sentence"],
                "concurrency": [1],
                "cache_states": ["warm"],
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


def test_profile_plan_expands_vantage_path_matrix_without_submitting_work():
    plan = plan_profile_matrix(_manifest())
    # Each workload matrix cell is measured over every explicitly allowed path.
    assert plan["cell_count"] == (8 + 1 + 1) * 2 * 3
    assert plan["planned_requests"] == plan["cell_count"] * 100
    assert plan["submission_count"] == 0
    assert plan["execution_status"] == "not_submitted"
    assert plan["authorization_required"] is True
    assert all(cell["sample_target"] >= 100 for cell in plan["cells"])
    remote_llm = next(
        cell for cell in plan["cells"]
        if cell["workload_class"] == "llm-27b" and cell["requester_vantage"] == "vantage-b"
    )
    assert remote_llm["execution_target"] == "worker-a"
    assert remote_llm["network_path_id"] == "b-to-a"
    assert remote_llm["runtime_revision"] == "runtime-a"


def test_profile_plan_rejects_invalid_network_path_or_unbounded_budget():
    bad = _manifest()
    bad["network_paths"][0]["execution_target"] = "unknown"
    with pytest.raises(ContractError, match="execution target"):
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
    assert all(request["need"]["profile_slot"] == 1 for request in authority.requests)
    assert all("gpu" not in request["need"] for request in authority.requests)
    assert authority.requests[0]["selector"]["profiling_vantage"] == authority.requests[0]["payload"]["cell"]["requester_vantage"]
    assert authority.requests[0]["payload"]["protected_service"] == _manifest()["protected_service"]


def test_profile_plan_rejects_implicit_or_unknown_workload_paths():
    bad = _manifest()
    del bad["workloads"][0]["network_path_ids"]
    with pytest.raises(ContractError, match="network_path_ids"):
        plan_profile_matrix(bad)

    bad = _manifest()
    bad["workloads"][0]["network_path_ids"].append("unknown")
    with pytest.raises(ContractError, match="unknown network path"):
        plan_profile_matrix(bad)

    bad = _manifest()
    bad["workloads"][1]["target_revisions"].pop("worker-b")
    with pytest.raises(ContractError, match="target_revisions"):
        plan_profile_matrix(bad)
