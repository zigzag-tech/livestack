"""The Harmony stream producers against a fake ingress that speaks the real frames.

Plain `python3 -m unittest` also runs these (stdlib only, no pytest fixtures); the planner parity test is
the one that needs the livestack_node package importable and says so loudly if it is not.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from streams_fake_ingress import FakeIngress  # noqa: E402

from livestack_node.streams import fleet_facts  # noqa: E402
from livestack_node.streams.benchday_streams import intent_partial  # noqa: E402,F401
from livestack_node.streams.host_facts import HostFacts, UnitDeclarations  # noqa: E402
from livestack_node.streams.lane import ScriptedBackend  # noqa: E402
from livestack_node.streams.request_authority import (RequestAuthority, reference_satisfies,  # noqa: E402
                                                      request_ledger)
from livestack_node.streams.runtime import FLEET, HOST, REQUEST, WORKLOAD, StreamsRuntime  # noqa: E402
from livestack_node.streams.workload_authority import (IntentLedger, WorkloadAuthority, classify_job,  # noqa: E402
                                                       rung_of)

DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
REF = {"ref": "oss://b/spec.json", "digest": "sha256:" + "a" * 64, "bytes": 120, "media": "application/json"}
HOST_KEYS = {"host", "units_digest", "units_mtime_ms", "units_count", "units_path", "changed_ms"}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logs = []

    def path(self, name):
        return os.path.join(self.dir, name)


class HostFactsTests(Base):
    def make(self, clock, publish=None):
        self.sent = []

        async def default(status, body, error):
            self.sent.append((status, body, error))
        return HostFacts("h1", UnitDeclarations((self.path("*units*.json"),)), self.path("state/host.json"),
                         publish or default, clock=clock, heartbeat_s=30, log=self.logs.append)

    def write(self, name, value, raw=None):
        with open(self.path(name), "w") as handle:
            handle.write(raw if raw is not None else json.dumps(value))

    async def test_digest_mtime_and_change_detection(self):
        now = [1_000_000]
        self.write("llm-units.json", [{"name": "a", "attributes": {"class": "llm"}}])
        self.write("llm-units-gpu0.json", [{"name": "b"}])
        self.write("llm-units.json.bak-20260101", "ignored: not a .json file")
        os.utime(self.path("llm-units.json"), (1000, 1000))
        os.utime(self.path("llm-units-gpu0.json"), (2000, 2000))
        facts = self.make(lambda: now[0])
        first = await facts.tick()
        self.assertEqual(set(first), HOST_KEYS)
        self.assertRegex(first["units_digest"], DIGEST)
        self.assertEqual((first["units_count"], first["units_mtime_ms"], first["changed_ms"]), (2, 2_000_000, 1_000_000))
        self.assertIsNone(await facts.tick(), "no publish between heartbeats when nothing changed")
        now[0] += 31_000
        again = await facts.tick()
        self.assertEqual(again["units_digest"], first["units_digest"])
        self.assertEqual(again["changed_ms"], first["changed_ms"], "a heartbeat is not a change")
        # a whitespace-only reformat is not drift; a real edit is, and it is published at once
        self.write("llm-units.json", None, raw=json.dumps([{"attributes": {"class": "llm"}, "name": "a"}], indent=4))
        self.assertIsNone(await facts.tick())
        now[0] += 1000
        self.write("llm-units.json", [{"name": "a", "attributes": {"class": "llm", "params_b": 27}}])
        changed = await facts.tick()
        self.assertNotEqual(changed["units_digest"], first["units_digest"])
        self.assertEqual(changed["changed_ms"], now[0])
        self.assertEqual(len(self.sent), 3)

    async def test_changed_ms_survives_a_restart_and_unreadable_files_are_a_named_failure(self):
        now = [5_000]
        self.write("llm-units.json", [1])
        facts = self.make(lambda: now[0])
        body = await facts.tick()
        now[0] = 99_000
        restarted = self.make(lambda: now[0])
        again = await restarted.tick()
        self.assertEqual(again["changed_ms"], body["changed_ms"])
        os.unlink(self.path("llm-units.json"))
        os.symlink(self.path("missing-target"), self.path("llm-units.json"))  # matches the glob, cannot be read
        self.assertIsNone(await restarted.tick())
        status, sent_body, error = self.sent[-1]
        self.assertEqual((status, sent_body, error["code"]), ("failed", None, "units_unreadable"))
        self.assertIn("llm-units.json", error["detail"])

    async def test_published_over_the_wire_in_shadow_and_active(self):
        fake = FakeIngress(self.path("i.sock"), shadow={HOST})
        await fake.start()
        self.addCleanup(fake.stop)
        self.write("llm-units.json", [{"name": "a"}])
        runtime = StreamsRuntime(self.path("i.sock"), self.logs.append, "h1")
        facts = HostFacts("h1", UnitDeclarations((self.path("*units*.json"),)), self.path("host.json"),
                          runtime.host_publisher(), log=self.logs.append)
        runtime.attach(host_facts=facts)
        runtime.start()
        self.assertTrue(await fake.settle(lambda: fake.publishes))
        await runtime.stop()
        self.assertEqual(fake.registers, [HOST])
        sent = fake.publishes[0]
        self.assertEqual((sent["contract"], sent["status"]), (HOST, "ok"))
        self.assertEqual(set(sent["body"]), HOST_KEYS)


FLAT_FLEET = {
    "units": [{"name": "llm_general", "kind": "llm", "residency": "SOFT_PIN", "resident": True, "busy": False, "vram_bytes": 21_000_000_000}],
    "devices": [{"id": "gpu1", "vram_used": 5, "vram_total": 24}], "degraded": ["policy on defaults"],
    "demand": [{"requires": "class=llm", "waiting_s": 3}], "demand_unmet": 1, "leases_active": 2}
FLAT_STATUS = {"node_state": "up", "membership": {"x": 1}, "kinds": [{"kind": "llm", "unit": "llm_general", "scope": "all"}]}
REAL_FLEET = {
    "vantage_host": "tower", "demand": {"entries": [{"kind": "llm", "selector": {"region": "na"}, "first_seen": 90.0}]},
    "quota": {"usage": {"media": 2}},
    "hosts": {"tower": {"nodes": [
        {"peer": "http://a/livestack", "state": "fresh", "kinds": ["llm"], "scope": None, "device_id": "tower/1",
         "device_mem": {"capacity": {"vram_bytes": 24_000}, "free": {"vram_bytes": 4_000}},
         "units": [{"kind": "llm_general", "priority": 30, "residency": 1, "footprint": {"vram_bytes": 21_000}, "resident": True,
                    "busy": True, "attributes": {"class": "llm"}}]},
        {"peer": "http://b/livestack", "state": "mia", "kinds": ["polyasr"]}]},
        "other": {"nodes": [{"peer": "http://c/livestack", "state": "fresh", "kinds": ["x"]}]}}}


class FleetFactsTests(unittest.TestCase):
    def test_parity_with_the_stage_1_adapter_on_its_own_input(self):
        adapter = fleet_facts.adapter_body(FLAT_FLEET, FLAT_STATUS)
        native = fleet_facts.fleet_body(FLAT_FLEET, FLAT_STATUS, 1_000.0)
        deviations = {"completed", "failed", "evicted", "degraded", "degraded_truncated", "host_id"}
        for key in adapter:
            if key not in deviations:
                self.assertEqual(native[key], adapter[key], key)
        for ring in ("completed", "failed", "evicted"):  # schema needs counts minItems 1; adapter's [] fails it
            self.assertEqual(native[ring]["counts"], [0])
            self.assertEqual(adapter[ring]["counts"], [])
        self.assertEqual(native["degraded"], ["policy on defaults", "counters_unavailable"])
        self.assertEqual(native["membership_state"], "fresh")

    def test_real_broker_shape_is_mapped_and_bounded(self):
        body = fleet_facts.fleet_body(REAL_FLEET, {"host_id": "tower", "membership": [], "peers": []}, 100.0)
        self.assertEqual(body["host_id"], "tower")
        self.assertEqual([u["name"] for u in body["units"]], ["llm_general"])
        self.assertEqual((body["units"][0]["residency"], body["units"][0]["busy"], body["units"][0]["vram_bytes"]), ("SOFT_PIN", True, 21_000))
        self.assertEqual(body["devices"], [{"id": "tower/1", "vram_total": 24_000, "vram_used": 20_000}])
        self.assertEqual(body["membership_state"], "fresh")
        self.assertEqual((body["demand"][0]["waiting_s"], body["demand_unmet"], body["leases_active"]), (10, 1, 2))
        self.assertEqual(body["kinds"], [{"kind": "llm", "unit": "llm", "scope": ""}, {"kind": "polyasr", "unit": "polyasr", "scope": ""}])
        self.assertLessEqual(len(json.dumps(body)), 4096)
        many = dict(REAL_FLEET, hosts={"tower": {"nodes": [{"peer": "p", "state": "fresh", "kinds": [f"k{i}" for i in range(40)],
                                                          "units": [{"kind": f"u{i}", "residency": 2, "footprint": {}} for i in range(40)]}]}})
        big = fleet_facts.fleet_body(many, {}, 0.0)
        self.assertEqual((len(big["units"]), big["units_truncated"], len(big["kinds"]), big["kinds_truncated"]), (24, 16, 24, 16))

    def test_job_body_maps_state_attempt_and_outcome(self):
        job = {"id": "j1", "owner": "o", "state": "running", "created": 10.0, "updated": 11.0, "spec": {"handler": "build", "selector": {"os": "linux"}},
               "attempts": [{"id": "a1", "worker": "w", "host": "h", "state": "running", "expires": 50.0}], "progress": {"p": 1}}
        body = fleet_facts.job_body(job, 0)
        self.assertEqual((body["state"], body["requires"], body["attempt"]["worker"], body["queued_ms"]), ("running", "os=linux", "w", 10_000))
        job.update(state="failed", result={"outcome": "product_failure", "result": {"error": "red"}})
        done = fleet_facts.job_body(job, 0)
        self.assertEqual((done["state"], done["outcome"]["kind"]), ("failed", "product"))
        self.assertIn("finished_ms", done)


class WorkloadTests(Base):
    async def start(self, ledger_path=None, epoch=7, backend=None, tick=0.03):
        fake = FakeIngress(self.path(f"w{epoch}.sock"), realm_epoch=epoch)
        await fake.start()
        self.addCleanup(fake.stop)
        runtime = StreamsRuntime(self.path(f"w{epoch}.sock"), self.logs.append, "h1", tick_s=tick)
        backend = backend or ScriptedBackend()
        ledger = IntentLedger(ledger_path or self.path("intents.sqlite"))
        authority = WorkloadAuthority(ledger, backend, runtime.intent_publisher(WORKLOAD), "harmony-workload:h1", log=self.logs.append)
        runtime.attach(workload=authority)
        runtime.start()
        self.addAsyncCleanup(runtime.stop)
        return fake, runtime, authority, backend

    async def submit(self, fake, intent_id, kind, **extra):
        return await fake.write(WORKLOAD, "submit", intent_id, {"body": {"kind": kind, "spec": REF, **extra}})

    async def test_ladder_to_done_and_every_rung_is_published_in_order(self):
        fake, _, _, _ = await self.start()
        first = await self.submit(fake, "i-ok", "ok")
        self.assertEqual((first["body"]["status"], first["epoch"], first["body"]["kind"]), ("admitted", 7, "ok"))
        self.assertTrue(await fake.settle(lambda: fake.bodies(WORKLOAD, "i-ok") and fake.bodies(WORKLOAD, "i-ok")[-1]["status"] == "done"))
        self.assertEqual([b["status"] for b in fake.bodies(WORKLOAD, "i-ok")], ["submitted", "admitted", "placed", "running", "done"])
        self.assertNotIn("attribution", fake.bodies(WORKLOAD, "i-ok")[-1])

    async def test_every_terminal_cause_carries_attribution_and_a_reason(self):
        fake, _, _, backend = await self.start()
        cases = {"fail-product": ("failed", "workload", "tests_red"), "fail-infra": ("failed", "infra", "worker_lost"),
                 "refuse-429": ("refused", "infra", "429")}
        for kind in cases:
            await self.submit(fake, kind, kind)
        for kind, (status, attribution, reason) in cases.items():
            self.assertTrue(await fake.settle(lambda: fake.bodies(WORKLOAD, kind)[-1]["status"] == status), kind)
            last = fake.bodies(WORKLOAD, kind)[-1]
            self.assertEqual(last["attribution"], attribution, kind)
            self.assertIn(reason, last["reason"], kind)
        # cancelled (before placed) and a job that vanished from the authority
        await self.submit(fake, "stay-1", "stay")
        cancelled = await fake.write(WORKLOAD, "cancel", "stay-1")
        self.assertEqual((cancelled["body"]["status"], cancelled["body"]["attribution"]), ("cancelled", "unknown"))
        await self.submit(fake, "stay-2", "stay")
        backend.jobs.pop("stay-2")
        self.assertTrue(await fake.settle(lambda: fake.bodies(WORKLOAD, "stay-2")[-1]["status"] == "failed"))
        self.assertEqual((fake.bodies(WORKLOAD, "stay-2")[-1]["attribution"], fake.bodies(WORKLOAD, "stay-2")[-1]["reason"]),
                         ("infra", "job_missing_from_authority"))

    def test_classification_table(self):
        lease = {"state": "failed", "reason": "execution lease expired",
                 "result": {"outcome": "infrastructure", "result": {"error": "abandoned", "detail": "execution lease expired"}}}
        limit = {"state": "failed", "reason": "cpu limit 4 exceeded", "result": {"outcome": "infrastructure", "cause": {"kind": "cpu"}, "result": {}}}
        transport = {"state": "failed", "result": {"outcome": "infrastructure", "result": {"error": "upload_reset"}}}
        self.assertEqual(classify_job(lease)["attribution"], "infra")
        self.assertEqual(classify_job(limit)["attribution"], "infra")  # landed choice: a typed cause is not the handler reporting failure
        self.assertEqual(classify_job(transport)["attribution"], "infra")
        self.assertEqual(classify_job({"state": "expired", "reason": "execution deadline expired"})["attribution"], "unknown")
        self.assertEqual(classify_job({"state": "failed"})["attribution"], "unknown")
        self.assertEqual(rung_of({"id": "j", "state": "queued"})[0], "admitted")

    async def test_idempotent_submit_returns_the_current_partial_and_a_different_body_is_refused(self):
        fake, _, _, _ = await self.start()
        first = await self.submit(fake, "same", "stay")
        again = await self.submit(fake, "same", "stay")
        self.assertEqual((again["seq"], again["body"]), (first["seq"], first["body"]))
        self.assertEqual(len([b for b in fake.bodies(WORKLOAD, "same") if b["status"] == "submitted"]), 1, "nothing re-run")
        other = await self.submit(fake, "same", "ok")
        self.assertEqual((other["body"]["status"], other["body"]["refusal"]), ("refused", "schema_invalid"))

    async def test_oversize_forbidden_and_step_are_refused_and_not_recorded(self):
        fake, _, authority, _ = await self.start()
        big = await fake.write(WORKLOAD, "submit", "big", {"body": {"kind": "ok", "selector": {f"k{i}": "x" * 400 for i in range(10)}}})
        self.assertEqual(big["body"]["refusal"], "oversize")
        inline = await fake.write(WORKLOAD, "submit", "inline", {"body": {"kind": "ok", "payload": {"source": "..."}}})
        self.assertEqual((inline["body"]["refusal"], inline["body"]["detail"]), ("forbidden_body_field", "body.payload"))
        extra = await fake.write(WORKLOAD, "submit", "extra", {"body": {"kind": "ok"}, "inline_bytes": "AAAA"})
        self.assertEqual(extra["body"]["refusal"], "forbidden_body_field")
        bad_ref = await fake.write(WORKLOAD, "submit", "ref", {"body": {"kind": "ok", "spec": {"ref": "x"}}})
        self.assertEqual(bad_ref["body"]["refusal"], "schema_invalid")
        step = await fake.write(WORKLOAD, "step", "i", {"body": {}})
        self.assertEqual(step["body"]["refusal"], "step_not_allowed")
        for name in ("big", "inline", "extra", "ref", "i"):
            self.assertIsNone(authority.ledger.get(name))
        for partial in (big, inline, extra, bad_ref):
            self.assertEqual(partial["body"]["status"], "refused")
            self.assertIn(partial["body"]["attribution"], ("workload", "infra", "unknown"))
            self.assertTrue(partial["body"]["reason"])

    async def test_a_restart_reannounces_every_non_terminal_intent_under_the_new_epoch(self):
        ledger_path = self.path("durable.sqlite")
        fake, runtime, authority, backend = await self.start(ledger_path, epoch=7)
        a = await self.submit(fake, "live-a", "stay")
        await self.submit(fake, "done-b", "ok")
        self.assertTrue(await fake.settle(lambda: fake.bodies(WORKLOAD, "done-b")[-1]["status"] == "done"))
        await runtime.stop()
        authority.ledger.close()
        fake2, _, authority2, _ = await self.start(ledger_path, epoch=8, backend=backend)
        ok = await fake2.settle(lambda: fake2.bodies(WORKLOAD, "live-a"))
        self.assertTrue(ok, self.logs[-8:])
        again = authority2.ledger.get("live-a")["partial"]
        self.assertEqual((again["epoch"], again["body"]["status"]), (8, "admitted"))
        self.assertGreater(again["seq"], a["seq"], "versions are monotonic across the restart")
        self.assertEqual(fake2.bodies(WORKLOAD, "done-b"), [], "a terminal intent is not re-announced")
        replay = await self.submit(fake2, "live-a", "stay")
        self.assertEqual(replay["body"]["status"], "admitted", "an idempotent re-submit returns the current partial")


UNITS = [
    {"name": "llm_general", "host": "h1", "resident": True, "footprint": 21,
     "attributes": {"class": "llm", "family": "qwen", "params_b": 27, "thinking": True}},
    {"name": "embed", "host": "h1", "resident": False, "footprint": 3, "attributes": {"class": "embed", "params_b": 0.6}},
]


class RequestTests(Base):
    def make(self, units=None, **kw):
        self.units = [dict(u) for u in (UNITS if units is None else units)]
        self.published = []

        async def publish(partial):
            self.published.append(partial["body"])
        authority = RequestAuthority(request_ledger(self.path("req.sqlite")), "h1", lambda scope, host: self.units,
                                     publish, "harmony-host:h1", log=self.logs.append, **kw)
        authority.epoch = 1
        return authority

    async def ask(self, authority, intent_id, require, scope="fleet", **body):
        class W:
            op, contract, principal = "submit", REQUEST, "agent:x"
        w = W()
        w.intent_id, w.payload = intent_id, {"body": {"scope": scope, "require": require, **body}}
        return (await authority.handle_write(w))["body"]

    async def test_inexpressible_names_the_clause_and_is_distinct_from_unsatisfiable(self):
        authority = self.make()
        missing = await self.ask(authority, "r1", {"class": "llm", "context_k": "[128,]"})
        self.assertEqual((missing["status"], missing["refusal"], missing["clause"]), ("refused", "requirement_inexpressible", "context_k"))
        self.assertIn("declared by no unit", missing["detail"])
        none_fit = await self.ask(authority, "r2", {"class": "llm", "params_b": "[100,200)"})
        self.assertEqual((none_fit["refusal"], none_fit.get("clause")), ("unsatisfiable", None))
        negated = await self.ask(authority, "r3", {"class": "llm", "family": {"not": "qwen"}})
        self.assertEqual(negated["refusal"], "unsatisfiable")
        for rid, require, clause in [("s1", {"family": "[1,2)"}, "family"), ("s2", {"params_b": "[20,30"}, "params_b"),
                                     ("s3", {"params_b": "[,]"}, "params_b"), ("s4", {"class": {"is": "llm"}}, "class"),
                                     ("s5", {"class": []}, "class"), ("s6", {}, "require")]:
            refused = await self.ask(authority, rid, require)
            self.assertEqual((refused["refusal"], refused["clause"]), ("requirement_inexpressible", clause), rid)
        prefer = await self.ask(authority, "p1", {"class": "llm"}, prefer={"vision": True})
        self.assertEqual((prefer["refusal"], prefer["clause"]), ("requirement_inexpressible", "prefer.vision"))

    async def test_the_four_value_shapes_are_evaluated_and_a_resident_match_is_placed_then_done(self):
        authority = self.make()
        for rid, require in [("a", {"class": "llm", "params_b": "[20,30)"}), ("b", {"family": ["qwen", "llama"]}),
                             ("c", {"class": {"not": "embed"}}), ("d", {"class": "llm", "params_b>=": 20}),
                             ("e", {"thinking": "true"})]:
            body = await self.ask(authority, rid, require)
            self.assertEqual((body["status"], body["unit"], body["host"]), ("done", "llm_general", "h1"), rid)
        self.assertEqual([b["status"] for b in self.published if b["intent_id"] == "a"], ["submitted", "evaluated", "placed", "done"])

    async def test_a_candidate_that_is_not_resident_stays_evaluated_until_it_is(self):
        authority = self.make()
        body = await self.ask(authority, "n", {"class": "embed"})
        self.assertEqual((body["status"], body["unit"]), ("evaluated", "embed"))
        self.units[1]["resident"] = True
        self.assertEqual(await authority.pump(), 1)
        self.assertEqual(authority.ledger.get("n")["partial"]["body"]["status"], "done")

    async def test_prefer_orders_candidates_and_scope_mismatch_and_idempotence(self):
        units = UNITS + [{"name": "llm_small", "host": "h1", "resident": True, "footprint": 5,
                          "attributes": {"class": "llm", "family": "llama", "params_b": 8}}]
        authority = self.make(units)
        body = await self.ask(authority, "pf", {"class": "llm"}, prefer={"family": "qwen"})
        self.assertEqual(body["unit"], "llm_general")
        body = await self.ask(authority, "pf2", {"class": "llm"})
        self.assertEqual(body["unit"], "llm_small", "no preference: resident, then the smaller footprint")
        mismatch = await self.ask(authority, "m", {"class": "llm"}, scope="host", host="elsewhere")
        self.assertEqual(mismatch["refusal"], "schema_invalid")
        again = await self.ask(authority, "pf", {"class": "llm"}, prefer={"family": "qwen"})
        self.assertEqual(again["status"], "done")
        self.assertEqual(len([b for b in self.published if b["intent_id"] == "pf" and b["status"] == "submitted"]), 1)

    async def test_over_the_wire_the_refusal_carries_clause_and_detail(self):
        fake = FakeIngress(self.path("r.sock"))
        await fake.start()
        self.addAsyncCleanup(fake.stop)
        runtime = StreamsRuntime(self.path("r.sock"), self.logs.append, "h1", tick_s=0.05)
        runtime.attach(request=self.make())
        runtime.request.publish = runtime.intent_publisher(REQUEST)
        runtime.start()
        self.addAsyncCleanup(runtime.stop)
        partial = await fake.write(REQUEST, "submit", "w1", {"body": {"scope": "fleet", "require": {"class": "llm", "context_k": "[128,]"}}})
        self.assertEqual((partial["body"]["refusal"], partial["body"]["clause"], partial["epoch"]), ("requirement_inexpressible", "context_k", 7))
        ok = await fake.settle(lambda: [b["status"] for b in fake.bodies(REQUEST, "w1")] == ["submitted", "refused"])
        self.assertTrue(ok, (fake.bodies(REQUEST, "w1"), self.logs))
        refused = await fake.write(REQUEST, "submit", "w2", {"body": {"scope": "fleet", "require": {"class": "llm"}, "prompt": "x"}})
        self.assertEqual(refused["body"]["refusal"], "forbidden_body_field")

    async def test_ledger_bounds_are_enforced(self):
        authority = self.make()
        authority.ledger.max_live = 2
        await self.ask(authority, "x1", {"class": "embed"})
        await self.ask(authority, "x2", {"class": "embed"})
        full = await self.ask(authority, "x3", {"class": "embed"})
        self.assertEqual(full["refusal"], "authority_unavailable")
        self.assertIn("intent_ledger_full", full["detail"])


class FleetOverTheWireTests(Base):
    async def test_fleet_is_registered_and_published_and_reports_shadow(self):
        fake = FakeIngress(self.path("f.sock"), shadow={FLEET})
        await fake.start()
        self.addAsyncCleanup(fake.stop)
        runtime = StreamsRuntime(self.path("f.sock"), self.logs.append, "h1", fleet_s=0.05)
        runtime.attach(fleet_reader=lambda: (REAL_FLEET, {"host_id": "tower", "membership": [], "peers": []}))
        runtime.start()
        self.addAsyncCleanup(runtime.stop)
        self.assertTrue(await fake.settle(lambda: fake.publishes))
        self.assertEqual(fake.registers, [FLEET])
        self.assertEqual(fake.publishes[0]["body"]["host_id"], "tower")
        self.assertTrue(any("in shadow" in line for line in self.logs))

    async def test_jobs_are_published_per_subject_on_change_only(self):
        fake = FakeIngress(self.path("j.sock"))
        await fake.start()
        self.addAsyncCleanup(fake.stop)
        runtime = StreamsRuntime(self.path("j.sock"), self.logs.append, "h1", tick_s=0.03)
        jobs = [{"id": "j1", "owner": "o", "state": "queued", "created": 1.0, "updated": 1.0, "spec": {"handler": "build"}, "attempts": []}]
        runtime.attach(jobs_reader=lambda: jobs)
        runtime.start()
        self.addAsyncCleanup(runtime.stop)
        self.assertTrue(await fake.settle(lambda: fake.publishes))
        await asyncio.sleep(0.2)
        self.assertEqual(len(fake.publishes), 1, "unchanged job is not republished before its heartbeat")
        self.assertEqual(fake.publishes[0]["subject"], {"job": "j1"})
        jobs[0].update(state="succeeded", updated=2.0, result={"outcome": "succeeded", "result": {}})
        self.assertTrue(await fake.settle(lambda: len(fake.publishes) == 2))
        self.assertEqual(fake.publishes[1]["body"]["state"], "succeeded")


class PlannerParityTests(unittest.TestCase):
    def test_reference_satisfies_matches_the_planner(self):
        try:
            from types import SimpleNamespace
            from livestack_node import planner
        except ImportError as error:  # loud, not silent: the parity claim is unproven if this fires
            self.fail(f"planner not importable, parity UNPROVEN: {error}")
        attrs = {"class": "llm", "family": "qwen", "params_b": 27, "thinking": True, "name": "x"}
        cases = [{"class": "llm"}, {"class": "embed"}, {"params_b>=": 20}, {"params_b>": 27}, {"params_b<=": 27}, {"params_b<": 27},
                 {"family": ["qwen", "llama"]}, {"family": ["llama"]}, {"family!=": "qwen"}, {"family!=": ["qwen", "x"]},
                 {"thinking": True}, {"thinking": False}, {"missing": 1}, {"family>=": 3}, {"class": "llm", "params_b>=": 28}]
        for requires in cases:
            self.assertEqual(reference_satisfies(attrs, requires),
                             planner._unit_satisfies(SimpleNamespace(attributes=attrs), requires), requires)


if __name__ == "__main__":
    unittest.main()
