import json
import urllib.error
import urllib.request
from threading import Thread

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


def test_workload_identity_endpoint_exports_only_fresh_principal_mappings(tmp_path):
    store = WorkloadStore(tmp_path / "identity.sqlite", handlers={"test.v1"})
    admin_token = "a" * 40
    mapped_token = "w" * 40
    unmapped_token = "u" * 40
    principals = [
        Principal("admin", admin_token, "admin", ("test.v1",)),
        Principal("mapped", mapped_token, "worker", worker="worker-a",
                  host="physical-a", benchday_host_id="host-a"),
        Principal("unmapped", unmapped_token, "worker", worker="worker-b",
                  host="physical-b"),
    ]
    server = WorkloadServer(("127.0.0.1", 0), store, principals,
                            identity_authority_id="workload-a")
    store.register("worker-a", "physical-a", "boot-a", _worker_report())
    store.register("worker-b", "physical-b", "boot-b", _worker_report())
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()

    def get(token=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/workloads/identity-facts",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        assert get()[0] == 401
        assert get(mapped_token)[0] == 403
        status, first = get(admin_token)
        assert status == 200
        assert first["sequence"] == 1
        assert first["source_state"] == {
            "status": "ok",
            "registered_workers": 2,
            "fresh_workers": 2,
            "published": 1,
            "unmapped": 1,
            "stale": 0,
        }
        [fact] = first["facts"]
        assert fact["authority"] == {"kind": "livestack", "id": "workload-a"}
        assert fact["subject"]["from"] == "livestack:worker:worker-a"
        assert fact["subject"]["to"] == "benchday:host:host-a"

        # A full next cut advances the source fence without creating work.
        status, second = get(admin_token)
        assert status == 200
        assert second["generation"] == first["generation"]
        assert second["sequence"] == 2
        assert store.list_jobs("admin") == []

        # Once the worker registration ages past the bounded TTL, its edge
        # disappears from the next complete cut rather than being restamped.
        with store.transaction() as db:
            db.execute("UPDATE workers SET seen=? WHERE id='worker-a'",
                       (store.clock() - 120,))
        status, stale = get(admin_token)
        assert status == 200
        assert stale["facts"] == []
        assert stale["source_state"]["stale"] == 1
    finally:
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()


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


def test_workload_identity_endpoint_fails_closed_without_authority_id(tmp_path):
    store = WorkloadStore(tmp_path / "missing-authority.sqlite", handlers={"test.v1"})
    admin_token = "a" * 40
    server = WorkloadServer(
        ("127.0.0.1", 0), store,
        [Principal("admin", admin_token, "admin", ("test.v1",))],
    )
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/v1/workloads/identity-facts",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req, timeout=5)
        assert refused.value.code == 503
        assert json.load(refused.value)["error"] == "identity_authority_not_configured"
    finally:
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()

