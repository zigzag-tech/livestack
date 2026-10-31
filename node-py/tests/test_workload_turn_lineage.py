import hashlib
import json
import sqlite3
from pathlib import Path
from threading import Thread
import time

import pytest

from livestack_node.workloads.http import Principal
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.job_changes import job_changes
from livestack_node.workloads.model import Limits, WorkloadError, submission
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.http import WorkloadServer


TURN = "5b1d5447-9261-4e33-a9b2-160ff52e8143"
SOURCE = {"kind": "overlord_turn", "turn_id": TURN}


def request(key="lineage", source=None):
    value = {
        "version": 1,
        "key": key,
        "handler": "test.v1",
        "input_digest": hashlib.sha256(b"fixture").hexdigest(),
        "need": {"cpu": 1},
    }
    if source is not None:
        value["source"] = source
    return value


def test_workload_submission_accepts_closed_turn_source_and_preserves_legacy_shape():
    parsed = submission(request(source=SOURCE), {"test.v1"}, Limits())
    assert parsed["source"] == SOURCE
    legacy = submission(request(), {"test.v1"}, Limits())
    assert "source" not in legacy


@pytest.mark.parametrize("source", [
    None,
    {},
    {"kind": "other", "turn_id": TURN},
    {"kind": "overlord_turn", "turn_id": TURN, "owner": "forged"},
    {"kind": "overlord_turn", "turn_id": "bad/turn"},
    {"kind": "overlord_turn", "turn_id": "é"},
    {"kind": "overlord_turn", "turn_id": "x" * 129},
])
def test_workload_submission_rejects_malformed_turn_source(source):
    value = request()
    value["source"] = source
    with pytest.raises(WorkloadError, match="source"):
        submission(value, {"test.v1"}, Limits())


def test_turn_source_capability_is_default_off_and_only_for_caller_or_admin():
    caller = Principal("caller", "c" * 32, "caller", ("test.v1",))
    assert caller.source_turns is False
    authorized = Principal("source-caller", "s" * 32, "caller", ("test.v1",), source_turns=True)
    assert authorized.source_turns is True
    with pytest.raises(ValueError, match="caller/admin"):
        Principal("worker", "w" * 32, "worker", worker="worker-1", host="host-1", source_turns=True)
    with pytest.raises(ValueError, match="job_reader"):
        Principal("reader", "r" * 32, "job_reader", source_turns=True)


def test_turn_source_is_part_of_durable_idempotency_and_survives_restart(tmp_path):
    path = tmp_path / "workloads.sqlite"
    store = WorkloadStore(path, handlers={"test.v1"})
    original = store.submit("owner", request(source=SOURCE))
    assert original["spec"]["source"] == SOURCE
    assert original["source_kind"] == "overlord_turn"
    assert original["source_turn_id"] == TURN

    reopened = WorkloadStore(path, handlers={"test.v1"})
    replay = reopened.submit("owner", request(source=SOURCE))
    assert replay["id"] == original["id"]
    assert replay["spec"]["source"] == SOURCE

    conflicting = {"kind": "overlord_turn", "turn_id": "1a318f48-30bd-4d9c-aeef-8d2759235a84"}
    with pytest.raises(WorkloadError, match="idempotency key"):
        reopened.submit("owner", request(source=conflicting))
    assert reopened.get("owner", original["id"])["spec"]["source"] == SOURCE


def test_old_database_migrates_to_nullable_unlinked_source_columns_and_source_index(tmp_path):
    path = tmp_path / "legacy.sqlite"
    schema_path = Path(__file__).parents[1] / "livestack_node" / "workloads" / "schema.sql"
    schema = schema_path.read_text()
    old_schema = schema.replace(
        "environment_handle TEXT, source_kind TEXT, source_turn_id TEXT,",
        "environment_handle TEXT,",
    )
    assert old_schema != schema
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO jobs(id,owner,request_key,request_hash,spec,state,created,updated,labels,retain) "
            "VALUES(?,?,?,?,?,'queued',1,1,'{}',0)",
            ("legacy-job", "owner", "legacy-key", "legacy-hash", json.dumps({"handler": "test.v1"})),
        )

    store = WorkloadStore(path, handlers={"test.v1"})
    legacy = store.get("owner", "legacy-job")
    assert legacy["source_kind"] is None
    assert legacy["source_turn_id"] is None
    with store.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE source_turn_id=? "
            "ORDER BY CAST(updated*1000 AS INTEGER),id LIMIT ?",
            (TURN, 257),
        ).fetchall()
        assert any("jobs_source_turn_changes" in row[3] for row in plan), plan


def test_worker_result_cannot_replace_source_and_retry_uses_its_claimed_boot(tmp_path):
    store = WorkloadStore(tmp_path / "retry.sqlite", handlers={"test.v1"})
    job = store.submit("owner", request(source=SOURCE))
    report = {
        "capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
        "handlers": ["test.v1"], "ready": True,
    }
    store.register("worker-a", "host-a", "boot-a", report)
    first = store.claim("worker-a", "boot-a", job_id=job["id"])
    spoof = {"kind": "overlord_turn", "turn_id": "worker-forged-turn"}
    ended = store.complete(
        "worker-a", "boot-a", first["attempt_id"], first["fence"],
        input_digest=job["spec"]["input_digest"], outcome="infrastructure",
        result={"source": spoof},
    )
    assert ended["state"] == "queued"
    assert ended["spec"]["source"] == SOURCE
    assert ended["source_turn_id"] == TURN

    store.register("worker-b", "host-b", "boot-b", report)
    second = store.claim("worker-b", "boot-b", job_id=job["id"])
    assert second["attempt_id"] != first["attempt_id"]
    page = job_changes(
        store, owner="owner", source_turn_id=TURN, updated_since=0,
        limit=10, inclusive=True, with_cursor=True, bounded=True,
    )
    row = next(item for item in page["jobs"] if item["id"] == job["id"])
    assert row["source_turn_id"] == TURN
    assert row["attempt"]["id"] == second["attempt_id"]
    assert row["attempt"]["worker"] == "worker-b"
    assert row["attempt"]["worker_session_id"] == "boot-b"


def test_exact_turn_reader_is_owner_scoped_bounded_and_exposes_safe_attempt_lineage(tmp_path):
    store = WorkloadStore(tmp_path / "reader.sqlite", handlers={"test.v1"})
    first = store.submit("owner-a", request("turn-a-1", SOURCE))
    store.submit("owner-a", request("turn-a-2", SOURCE))
    store.submit("owner-a", request("turn-other", {"kind": "overlord_turn", "turn_id": "other-turn"}))
    store.submit("owner-b", request("turn-owner-b", SOURCE))
    store.register("worker-a", "host-a", "worker-boot-a", {
        "capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
        "handlers": ["test.v1"], "ready": True,
    })
    assignment = store.claim("worker-a", "worker-boot-a", job_id=first["id"])
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET state='failed',updated=?,result=? WHERE id=?",
            (time.time(), json.dumps({"outcome": "product_failure", "result": {
                "error": "private command output", "detail": "private handler prose"}}), first["id"]),
        )
        db.execute("UPDATE attempts SET progress=? WHERE id=?",
                   (json.dumps({"message": "private progress text"}), assignment["attempt_id"]))

    server = WorkloadServer(("127.0.0.1", 0), store, [
        Principal("owner-a", "r" * 32, "job_reader"),
    ])
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = WorkloadClient(f"http://127.0.0.1:{server.server_port}", "r" * 32)
    try:
        full = client.list_job_changes_for_turn(
            expected_owner="owner-a", source_turn_id=TURN, limit=10,
        )
        assert full["complete"] is True
        assert len(full["jobs"]) == 2
        row = next(job for job in full["jobs"] if job["id"] == first["id"])
        assert row["source_kind"] == "overlord_turn"
        assert row["source_turn_id"] == TURN
        assert row["state"] == "failed"
        assert row["current_explanation"] == {"code": "failed", "observed_ms": row["updated_ms"]}
        assert row["attempt"]["id"] == assignment["attempt_id"]
        assert row["attempt"]["worker"] == "worker-a"
        assert row["attempt"]["worker_session_id"] == "worker-boot-a"
        assert "outcome" not in row
        assert "private command output" not in json.dumps(full)
        assert "private handler prose" not in json.dumps(full)
        assert "private progress text" not in json.dumps(full)

        page = client.list_job_changes_for_turn(
            expected_owner="owner-a", source_turn_id=TURN, limit=1,
        )
        assert page["complete"] is False
        assert page["truncated"] == 1
        assert len(page["jobs"]) == 1

        cursor = page["next_cursor"]
        second = client.list_job_changes_for_turn(
            expected_owner="owner-a", source_turn_id=TURN, limit=1,
            updated_since=cursor["updated_since"], after_job_id=cursor["after_job_id"],
        )
        assert second["complete"] is True
        assert second["truncated"] == 0
        assert len(second["jobs"]) == 1
        assert second["jobs"][0]["source_turn_id"] == TURN

        empty = client.list_job_changes_for_turn(
            expected_owner="owner-a", source_turn_id="no-such-turn",
        )
        assert empty["complete"] is True
        assert empty["jobs"] == []
        with pytest.raises(WorkloadError, match="invalid or oversized"):
            client.list_job_changes_for_turn(expected_owner="owner-b", source_turn_id=TURN)

        real_request = client.request
        def mismatched_source(route):
            response = real_request(route)
            if response["jobs"]:
                response["jobs"][0]["source_turn_id"] = "another-turn"
            return response
        client.request = mismatched_source
        with pytest.raises(WorkloadError, match="source mismatch"):
            client.list_job_changes_for_turn(expected_owner="owner-a", source_turn_id=TURN)

        def malformed_explanation(route):
            response = real_request(route)
            if response["jobs"]:
                response["jobs"][0]["current_explanation"] = {
                    "code": "private handler output", "observed_ms": response["jobs"][0]["updated_ms"],
                }
            return response
        client.request = malformed_explanation
        with pytest.raises(WorkloadError, match="lineage explanation"):
            client.list_job_changes_for_turn(expected_owner="owner-a", source_turn_id=TURN)
    finally:
        client.close()
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()
