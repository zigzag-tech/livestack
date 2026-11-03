import pytest

from livestack_node.workloads.config import AuthorityConfig
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore


def test_workload_authority_config_accepts_explicit_identity_authority_id():
    config = AuthorityConfig.model_validate({
        "state_dir": "/tmp/livestack-identity",
        "handlers": ["test.v1"],
        "principals": [],
        "identity_authority_id": "workload-a",
    })
    assert config.identity_authority_id == "workload-a"


def _worker_report(extra=None):
    report = {
        "capacity": {"cpu": 2},
        "available": {"cpu": 2},
        "labels": {},
        "handlers": ["test.v1"],
        "ready": True,
    }
    if extra:
        report.update(extra)
    return report


def test_worker_report_cannot_choose_its_benchday_host_mapping(tmp_path):
    store = WorkloadStore(tmp_path / "invalid-report.sqlite", handlers={"test.v1"})
    with pytest.raises(WorkloadError, match="invalid worker report"):
        store.register(
            "worker-a",
            "physical-a",
            "boot-a",
            _worker_report({"benchday_host_id": "host-spoof"}),
        )


def test_worker_principal_host_mapping_is_an_immutable_binding(tmp_path):
    store = WorkloadStore(tmp_path / "reload.sqlite", handlers={"test.v1"})
    original = Principal("worker", "w" * 40, "worker", worker="worker-a",
                         host="physical-a", benchday_host_id="host-a")
    server = WorkloadServer(("127.0.0.1", 0), store, [original])
    changed = Principal("worker", "w" * 40, "worker", worker="worker-a",
                        host="physical-a", benchday_host_id="host-b")
    try:
        with pytest.raises(ValueError, match="principal_binding_changed"):
            server.replace_principals([changed])
    finally:
        server.server_close()
