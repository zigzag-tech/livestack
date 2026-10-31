"""The production workload-authority factory, real stores, and framed fake ingress.

The fixture deliberately calls ``start_for_workload_authority`` without a
custom resolver. All authority, job, blob, and decision-ledger files live in a
temporary directory; the fake ingress is an isolated Unix socket.
"""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from urllib.request import Request, urlopen
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from streams_fake_ingress import FakeIngress  # noqa: E402

from livestack_node import hostd_streams  # noqa: E402
from livestack_node.ledger import JsonlLedger  # noqa: E402
from livestack_node.workloads.blobs import BlobStore  # noqa: E402
from livestack_node.workloads.http import Principal, WorkloadServer  # noqa: E402
from livestack_node.workloads.model import WorkloadError, encode  # noqa: E402
from livestack_node.workloads.store import WorkloadStore  # noqa: E402


CONTRACT = "harmony.workload/1"
INPUT = b"temporary-workload-input"


class WorkloadAuthorityIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workload-intent-authority-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.decision_path = root / "workload-decisions.jsonl"
        self.decision_ledger = JsonlLedger(str(self.decision_path), max_bytes=1024 * 1024,
                                           max_files=2, log=lambda _message: None)
        self.store = WorkloadStore(root / "workloads.sqlite", handlers={"test.v1"},
                                   decision_ledger=self.decision_ledger,
                                   decision_emitter_id="fixture-workload-authority")
        self.principals = (
            Principal("caller-a", "a" * 40, "caller", handlers=("test.v1", "uninstalled.v1")),
            Principal("caller-b", "b" * 40, "caller", handlers=("test.v1",)),
        )
        self.store.bind_principals(self.principals)
        self.blobs = BlobStore(self.store, root / "objects", retention_seconds=None)

        self.http = WorkloadServer(("127.0.0.1", 0), self.store, self.principals, blobs=self.blobs)
        self.http_thread = Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        def stop_http():
            self.http.shutdown()
            self.http_thread.join(timeout=5)
            self.http.server_close()
        self.addCleanup(stop_http)

        self.ingress = FakeIngress(str(root / "ingress.sock"), realm_epoch=7)
        await self.ingress.start()
        self.addAsyncCleanup(self.ingress.stop)

        env = {"HARMONY_STREAMS": "1", "HARMONY_STREAMS_SOCKET": str(root / "ingress.sock")}
        with patch.dict(os.environ, env), patch.object(hostd_streams, "enabled", return_value=True), \
                patch.object(hostd_streams, "_run_in_thread",
                             side_effect=lambda runtime, _log: runtime.start()):
            self.runtime = hostd_streams.start_for_workload_authority(
                self.store, "fixture-host", str(root / "authority"), blobs=self.blobs,
                log=lambda _message: None)
        async def stop_runtime():
            await self.runtime.stop()
            self.runtime.workload.ledger.close()
        self.addAsyncCleanup(stop_runtime)
        self.assertTrue(await self.ingress.settle(lambda: CONTRACT in self.ingress.serving),
                        "the factory must serve the workload contract over framed ingress")

    def reference(self, raw, *, owner="caller-a", media="application/json", declared_bytes=None):
        digest = hashlib.sha256(raw).hexdigest()
        self.blobs.put(owner, digest, len(raw), io.BytesIO(raw))
        return {"ref": f"cas://{digest}", "digest": f"sha256:{digest}",
                "bytes": len(raw) if declared_bytes is None else declared_bytes, "media": media}

    def request(self, *, handler="test.v1", input_digest=None):
        return {"version": 1, "key": "caller-chosen-key", "handler": handler,
                "input_digest": input_digest or hashlib.sha256(INPUT).hexdigest(),
                "need": {"cpu": 1}, "payload": {"source": "fixture"}}

    async def submit(self, intent_id, spec, *, principal="caller-a", owner=None):
        body = {"kind": "fixture", "spec": spec}
        if owner is not None:
            body["owner"] = owner
        return await self.ingress.write(CONTRACT, "submit", intent_id, {"body": body}, principal=principal)

    def counts(self):
        with self.store.transaction() as db:
            jobs = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            attempts = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
        return jobs, attempts

    def records(self):
        if not self.decision_path.exists():
            return []
        return [json.loads(line) for line in self.decision_path.read_text().splitlines() if line]

    def fetch_result(self, job_id, *, token="a" * 40):
        request = Request(f"http://127.0.0.1:{self.http.server_port}/v1/workloads/jobs/{job_id}/result",
                          headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=5) as response:
            raw = response.read()
        return json.loads(raw), raw

    def fetch_job(self, job_id, *, token="a" * 40):
        request = Request(f"http://127.0.0.1:{self.http.server_port}/v1/workloads/jobs/{job_id}",
                          headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    async def test_factory_resolves_owner_cas_and_replay_reaches_one_real_admission(self):
        self.blobs.put("caller-a", hashlib.sha256(INPUT).hexdigest(), len(INPUT), io.BytesIO(INPUT))
        raw = json.dumps(self.request()).encode("utf-8")
        ref = self.reference(raw)

        first = await self.submit("intent-valid", ref)
        self.assertEqual(first["body"]["status"], "admitted")
        record = self.runtime.workload.ledger.get("intent-valid")
        self.assertEqual(record["owner"], "caller-a")
        self.assertEqual(record["partial"], first)
        job_id = record["job"]
        job = self.store.get("caller-a", job_id)
        self.assertEqual(job["spec"]["key"], "intent-valid")
        self.assertEqual(job["spec"]["handler"], "test.v1")
        self.assertEqual(self.counts(), (1, 0))

        replay = await self.submit("intent-valid", ref)
        self.assertEqual(replay, first, "same-owner replay returns the current partial")
        self.assertEqual(self.runtime.workload.ledger.get("intent-valid")["job"], job_id)
        self.assertEqual(self.counts(), (1, 0), "replay must not make a second job or attempt")
        self.assertEqual(self.records(), [], "replay cannot create a placement decision")

        self.store.register("fixture-worker", "fixture-host", "boot-1", {
            "capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
            "handlers": ["test.v1"], "ready": True,
        })
        assignment = self.store.claim("fixture-worker", "boot-1", job_id=job_id)
        self.assertEqual(assignment["job_id"], job_id)
        self.assertEqual(self.counts(), (1, 1))
        self.assertTrue(await self.ingress.settle(
            lambda: self.ingress.bodies(CONTRACT, "intent-valid")[-1]["status"] == "placed"))
        self.assertEqual(self.runtime.workload.ledger.get("intent-valid")["job"], job_id)
        decisions = [row for row in self.records()
                     if row.get("decision") == "admit" and row.get("request", {}).get("job_id") == job_id]
        self.assertEqual(len(decisions), 1, "one real WorkloadStore placement decision is recorded")
        self.assertEqual(decisions[0]["request"]["owner"], "caller-a")
        self.assertEqual(decisions[0]["request"]["attempt_id"], assignment["attempt_id"])

    async def test_strict_reference_and_handler_refusals_are_attributed_without_jobs(self):
        self.blobs.put("caller-a", hashlib.sha256(INPUT).hexdigest(), len(INPUT), io.BytesIO(INPUT))
        cases = []

        cases.append(("missing", {"ref": "cas://missing", "digest": "sha256:" + "1" * 64,
                                   "bytes": 2, "media": "application/json"}, "workload"))
        foreign_raw = json.dumps(self.request()).encode()
        foreign = self.reference(foreign_raw, owner="caller-a")
        cases.append(("foreign", foreign, "workload", "caller-b"))
        cases.append(("wrong-media", self.reference(json.dumps(self.request()).encode(), media="text/plain"),
                      "workload"))
        size_raw = json.dumps(self.request()).encode()
        cases.append(("wrong-size", self.reference(size_raw, declared_bytes=len(size_raw) + 1), "workload"))

        corrupt_raw = json.dumps(self.request()).encode()
        corrupt_ref = self.reference(corrupt_raw)
        digest = corrupt_ref["digest"].removeprefix("sha256:")
        (self.blobs.root / digest).write_bytes(b"!" * len(corrupt_raw))
        cases.append(("digest-mismatch", corrupt_ref, "workload"))
        cases.append(("invalid-utf8", self.reference(b"\xff"), "workload"))
        cases.append(("non-object", self.reference(b"[]"), "workload"))
        cases.append(("over-limit", self.reference(b"{" + b" " * 1024 * 1024 + b"}") , "workload"))
        cases.append(("uninstalled-handler", self.reference(json.dumps(
            self.request(handler="uninstalled.v1")).encode()), "workload"))
        cases.append(("invalid-input", self.reference(json.dumps(self.request(
            input_digest="f" * 64)).encode()), "workload"))

        for case in cases:
            name, ref, attribution, *owner_args = case
            principal = owner_args[0] if owner_args else "caller-a"
            before = self.counts()
            decisions_before = self.records()
            partial = await self.submit("reject-" + name, ref, principal=principal)
            self.assertEqual(partial["body"]["status"], "refused", name)
            self.assertEqual(partial["body"]["attribution"], attribution, name)
            self.assertTrue(partial["body"]["reason"], name)
            self.assertLessEqual(len(partial["body"]["reason"]), 240, name)
            self.assertEqual(self.runtime.workload.ledger.get("reject-" + name)["partial"], partial, name)
            self.assertEqual(self.counts(), before, name)
            self.assertEqual(self.records(), decisions_before, name)

        before = self.counts()
        decisions_before = self.records()
        owner_spoof = await self.submit("reject-owner-spoof", foreign, principal="caller-b", owner="caller-a")
        self.assertEqual(owner_spoof["body"]["status"], "refused")
        self.assertEqual(owner_spoof["body"]["attribution"], "workload")
        self.assertIsNone(self.runtime.workload.ledger.get("reject-owner-spoof"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.records(), decisions_before)

    async def test_backend_outage_cancel_owner_and_replay_keep_terminal_records(self):
        self.blobs.put("caller-a", hashlib.sha256(INPUT).hexdigest(), len(INPUT), io.BytesIO(INPUT))
        request = self.request()
        outage_ref = self.reference(json.dumps(request).encode())
        before = self.counts()
        with patch.object(self.store, "submit", side_effect=WorkloadError("store unavailable", 503)):
            refused = await self.submit("intent-outage", outage_ref)
        self.assertEqual((refused["body"]["status"], refused["body"]["attribution"]), ("refused", "infra"))
        outage_record = self.runtime.workload.ledger.get("intent-outage")
        self.assertTrue(outage_record["terminal"])
        self.assertEqual(outage_record["partial"], refused)
        self.assertEqual(self.counts(), before)

        cancel_ref = self.reference(json.dumps(request).encode())
        admitted = await self.submit("intent-cancel", cancel_ref)
        self.assertEqual(admitted["body"]["status"], "admitted")
        job_id = self.runtime.workload.ledger.get("intent-cancel")["job"]
        unauthorized = await self.ingress.write(CONTRACT, "cancel", "intent-cancel", {"body": {}},
                                                principal="caller-b")
        self.assertEqual(unauthorized["body"]["status"], "refused")
        self.assertEqual(unauthorized["body"]["attribution"], "workload")
        self.assertFalse(self.runtime.workload.ledger.get("intent-cancel")["terminal"])
        self.assertEqual(self.store.get("caller-a", job_id)["state"], "queued")

        cancelled = await self.ingress.write(CONTRACT, "cancel", "intent-cancel", {"body": {}},
                                             principal="caller-a")
        self.assertEqual((cancelled["body"]["status"], cancelled["body"]["attribution"]),
                         ("cancelled", "unknown"))
        self.assertEqual(cancelled["body"]["reason"], "cancelled_by_requester_before_placed")
        terminal = self.runtime.workload.ledger.get("intent-cancel")
        self.assertTrue(terminal["terminal"])
        self.assertEqual(terminal["owner"], "caller-a")
        self.assertEqual(terminal["partial"], cancelled)
        again = await self.ingress.write(CONTRACT, "cancel", "intent-cancel", {"body": {}},
                                         principal="caller-a")
        self.assertEqual(again, cancelled)
        self.assertEqual(self.runtime.workload.ledger.get("intent-cancel")["partial"], cancelled)
        self.assertEqual(self.store.get("caller-a", job_id)["state"], "cancelled")

    async def test_success_partial_and_authenticated_result_manifest_share_receipt_metadata(self):
        self.blobs.put("caller-a", hashlib.sha256(INPUT).hexdigest(), len(INPUT), io.BytesIO(INPUT))
        ref = self.reference(json.dumps(self.request()).encode())
        admitted = await self.submit("intent-success", ref)
        self.assertEqual(admitted["body"]["status"], "admitted")
        job_id = self.runtime.workload.ledger.get("intent-success")["job"]

        self.store.register("fixture-worker", "fixture-host", "boot-success", {
            "capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
            "handlers": ["test.v1"], "ready": True,
        })
        assignment = self.store.claim("fixture-worker", "boot-success", job_id=job_id)
        self.assertEqual(assignment["job_id"], job_id)
        completed = self.store.complete(
            "fixture-worker", "boot-success", assignment["attempt_id"], assignment["fence"],
            input_digest=hashlib.sha256(INPUT).hexdigest(), outcome="succeeded",
            result={"exit_code": 0, "resources": {"oom_kill": 0, "pids_max_events": 0}, "artifacts": []})
        self.assertEqual(completed["state"], "succeeded")

        self.assertTrue(await self.ingress.settle(
            lambda: self.ingress.bodies(CONTRACT, "intent-success")[-1]["status"] == "done"))
        partial = self.ingress.bodies(CONTRACT, "intent-success")[-1]
        self.assertEqual((partial["attribution"], partial["reason"]),
                         ("workload", "handler_exit_code_zero"))
        output = partial["output"]
        manifest, raw = self.fetch_result(job_id)
        self.assertEqual(output["digest"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(output["bytes"], len(raw))
        self.assertEqual((manifest["attribution"], manifest["reason"]),
                         (partial["attribution"], partial["reason"]))
        self.assertEqual((manifest["result"]["attribution"], manifest["result"]["reason"]),
                         (partial["attribution"], partial["reason"]))
        self.assertEqual(manifest["cause"], completed["cause"])
        self.assertEqual(manifest["cause"]["evidence"], {
            "exit_code": 0, "oom_kill": 0, "pids_max_events": 0})

        # Recreate a pre-metadata committed row without replaying worker completion.
        legacy_completion = {key: value for key, value in completed["result"].items()
                             if key not in ("attribution", "reason", "cause")}
        with self.store.transaction() as db:
            db.execute("UPDATE attempts SET result=? WHERE id=?",
                       (encode(legacy_completion, self.store.limits.record_bytes), assignment["attempt_id"]))
            db.execute("UPDATE jobs SET result=?,reason=NULL,cause=? WHERE id=?",
                       (encode(legacy_completion, self.store.limits.record_bytes),
                        encode({"kind": "succeeded", "retry": "no", "evidence": {}}, 2048), job_id))
        legacy_job = self.fetch_job(job_id)
        legacy_manifest, legacy_raw = self.fetch_result(job_id)
        self.assertEqual((legacy_job["result"]["attribution"], legacy_job["result"]["reason"]),
                         (partial["attribution"], partial["reason"]))
        self.assertEqual((legacy_job["cause"]["kind"], legacy_job["cause"]["evidence"]),
                         ("succeeded", {"exit_code": 0, "oom_kill": 0, "pids_max_events": 0}))
        self.assertEqual((legacy_manifest["attribution"], legacy_manifest["reason"]),
                         (legacy_job["result"]["attribution"], legacy_job["result"]["reason"]))
        self.assertEqual(legacy_manifest["cause"], legacy_job["cause"])
        self.assertEqual(output["digest"], "sha256:" + hashlib.sha256(legacy_raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
