import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from streams_fake_ingress import FakeIngress  # noqa: E402

from livestack_node.identity_facts import IdentitySnapshotPublisher, fleet_identity_cut  # noqa: E402
from livestack_node.workloads.http import Principal, WorkloadServer  # noqa: E402
from livestack_node.workloads.store import WorkloadStore  # noqa: E402
from livestack_node.streams import identity_facts as pagewire  # noqa: E402
from livestack_node.streams.identity_facts import (  # noqa: E402
    FLEET_IDENTITY_FACTS,
    IDENTITY_CUT_REFRESH_S,
    WORKLOAD_IDENTITY_FACTS,
    IdentityCutError,
    canonical,
    pack_identity_cut,
)
from livestack_node.streams.runtime import FLEET, HOST, REQUEST, StreamsRuntime  # noqa: E402


FLEET_AUTHORITY = "fleet-a"
WORKLOAD_AUTHORITY = "workload-a"
NOW_MS = 1_800_000_000_000


def relations(count, *, namespace="harmony:node", host_id="host-a", id_pad=0):
    return [{"resource_namespace": namespace,
             "resource_id": f"node-{index}-" + "x" * id_pad,
             "host_id": host_id,
             "observed_at_ms": NOW_MS - 100,
             "ttl_ms": 60_000}
            for index in range(count)]


def snapshot(items, authority_id=FLEET_AUTHORITY):
    publisher = IdentitySnapshotPublisher("livestack", authority_id)
    publisher.generation = "g" * 32
    return publisher.snapshot(items, now_ms=NOW_MS)


def pack_fleet(cut):
    return pack_identity_cut(cut, contract=FLEET_IDENTITY_FACTS,
                             authority_id=FLEET_AUTHORITY,
                             resource_namespace="harmony:node")


class IdentityCutPackingTests(unittest.TestCase):
    def test_fleet_stream_helper_advances_one_publisher_fence(self):
        class Broker:
            @staticmethod
            def fleet_view():
                return {}

            @staticmethod
            def identity_relations():
                return relations(1), {"status": "ok", "published": 1}

        publisher = IdentitySnapshotPublisher("livestack", FLEET_AUTHORITY)
        first_cut = fleet_identity_cut(Broker(), publisher)
        second_cut = fleet_identity_cut(Broker(), publisher)
        self.assertEqual(first_cut["generation"], second_cut["generation"])
        self.assertEqual((first_cut["sequence"], second_cut["sequence"]), (1, 2))
        self.assertEqual(first_cut["source_state"]["published"], 1)

    def test_empty_cut_is_one_empty_page(self):
        [page] = pack_fleet(snapshot([]))
        self.assertEqual(page, {"v": 1, "generation": "g" * 32, "sequence": 1,
                                "observed_at_ms": NOW_MS, "page_index": 0,
                                "page_count": 1, "facts": []})

    def test_maximum_fact_count_pages_in_order_with_one_shared_fence(self):
        cut = snapshot(relations(4096))
        pages = pack_fleet(cut)
        self.assertGreater(len(pages), 1)
        self.assertLessEqual(len(pages), 128)
        self.assertEqual([p["page_index"] for p in pages], list(range(len(pages))))
        self.assertEqual({p["page_count"] for p in pages}, {len(pages)})
        self.assertEqual({(p["generation"], p["sequence"], p["observed_at_ms"])
                          for p in pages}, {(cut["generation"], cut["sequence"], cut["observed_at_ms"])})
        flattened = [fact for page in pages for fact in page["facts"]]
        self.assertEqual(flattened, cut["facts"])
        self.assertEqual(len(flattened), 4096)
        for index, page in enumerate(pages):
            self.assertLessEqual(pagewire._complete_partial_size(
                FLEET_IDENTITY_FACTS, index, page), pagewire.MAX_IDENTITY_PARTIAL_BYTES)

    def test_one_fact_over_count_limit_is_rejected(self):
        cut = snapshot(relations(4097))
        with self.assertRaisesRegex(IdentityCutError, "identity_cut_fact_count_exceeded"):
            pack_fleet(cut)

    def test_exact_two_mib_canonical_snapshot_is_accepted_and_one_more_byte_is_refused(self):
        count = 4096
        base = [f"node-{index}-" for index in range(count)]

        def make_with_host_length(length, extra=0):
            items = [{"resource_namespace": "harmony:node", "resource_id": name,
                      "host_id": "h" * (length + (index < extra)),
                      "observed_at_ms": NOW_MS - 100, "ttl_ms": 60_000}
                     for index, name in enumerate(base)]
            return snapshot(items)

        low, high = 0, 220
        while low < high:
            middle = (low + high + 1) // 2
            candidate = make_with_host_length(middle)
            size = len(canonical({k: candidate[k] for k in
                                  ("v", "generation", "sequence", "observed_at_ms", "facts")}))
            if size <= pagewire.MAX_IDENTITY_SNAPSHOT_BYTES:
                low = middle
            else:
                high = middle - 1
        under = make_with_host_length(low)
        under_size = len(canonical({k: under[k] for k in
                                    ("v", "generation", "sequence", "observed_at_ms", "facts")}))
        self.assertLessEqual(under_size, pagewire.MAX_IDENTITY_SNAPSHOT_BYTES)
        delta = pagewire.MAX_IDENTITY_SNAPSHOT_BYTES - under_size
        exact = make_with_host_length(low, delta)
        exact_size = len(canonical({k: exact[k] for k in
                                    ("v", "generation", "sequence", "observed_at_ms", "facts")}))
        self.assertEqual(exact_size, pagewire.MAX_IDENTITY_SNAPSHOT_BYTES)
        self.assertLessEqual(len(pack_fleet(exact)), 128)

        over = make_with_host_length(low, delta + 1)
        over_size = len(canonical({k: over[k] for k in
                                   ("v", "generation", "sequence", "observed_at_ms", "facts")}))
        self.assertEqual(over_size, pagewire.MAX_IDENTITY_SNAPSHOT_BYTES + 1)
        with self.assertRaisesRegex(IdentityCutError, "identity_cut_snapshot_bytes_exceeded"):
            pack_fleet(over)

    def test_multibyte_identifiers_are_measured_as_utf8_bytes(self):
        cut = snapshot([{"resource_namespace": "harmony:node",
                         "resource_id": "é" * 110, "host_id": "🧡" * 55,
                         "observed_at_ms": NOW_MS - 100, "ttl_ms": 60_000}])
        pages = pack_fleet(cut)
        self.assertEqual(len(pages), 1)
        self.assertLessEqual(pagewire._complete_partial_size(
            FLEET_IDENTITY_FACTS, 0, pages[0]), pagewire.MAX_IDENTITY_PARTIAL_BYTES)

    def test_page_count_limit_rejects_before_returning_any_candidate_pages(self):
        cut = snapshot(relations(100))
        with patch.object(pagewire, "MAX_IDENTITY_PAGES", 1):
            with self.assertRaisesRegex(IdentityCutError, "identity_cut_page_count_exceeded"):
                pack_fleet(cut)

    def test_shorter_cut_emits_only_new_page_indexes(self):
        publisher = IdentitySnapshotPublisher("livestack", FLEET_AUTHORITY)
        previous = pack_identity_cut(
            publisher.snapshot(relations(1000), now_ms=NOW_MS),
            contract=FLEET_IDENTITY_FACTS, authority_id=FLEET_AUTHORITY,
            resource_namespace="harmony:node")
        current = pack_identity_cut(
            publisher.snapshot(relations(1), now_ms=NOW_MS + 1),
            contract=FLEET_IDENTITY_FACTS, authority_id=FLEET_AUTHORITY,
            resource_namespace="harmony:node")
        self.assertGreater(len(previous), 1)
        self.assertEqual([page["page_index"] for page in current], [0])
        self.assertEqual(current[0]["page_count"], 1)
        self.assertGreater(current[0]["sequence"], previous[0]["sequence"])

    def test_authority_namespace_and_inner_fence_are_required(self):
        cut = snapshot(relations(1))
        bad_authority = dict(cut)
        bad_authority["facts"] = [dict(cut["facts"][0], authority={"kind": "livestack", "id": "other"})]
        with self.assertRaisesRegex(IdentityCutError, "identity_fact_authority_or_scope_invalid"):
            pack_fleet(bad_authority)
        bad_fence = dict(cut)
        bad_fence["facts"] = [dict(cut["facts"][0], fence={"generation": "old", "sequence": 1})]
        with self.assertRaisesRegex(IdentityCutError, "identity_fact_fence_or_ttl_invalid"):
            pack_fleet(bad_fence)


class IdentityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_invalid_cut_is_rejected_before_first_publish(self):
        sent = []
        cut = snapshot(relations(1))
        cut["facts"] = [dict(cut["facts"][0], authority={"kind": "livestack", "id": "wrong"})]
        runtime = StreamsRuntime(None, lambda _message: None, "h1")
        runtime.attach(identity_reader=lambda: cut, identity_contract=FLEET_IDENTITY_FACTS,
                       identity_authority_id=FLEET_AUTHORITY, identity_namespace="harmony:node")

        async def publish(*args):
            sent.append(args)
            return {"shadowed": False}
        runtime.publish_state = publish
        with self.assertRaisesRegex(RuntimeError, "identity_fact_cut_rejected"):
            await runtime._identity_tick()
        self.assertEqual(sent, [])

    async def test_valid_cut_publishes_stable_indexed_pages(self):
        sent = []
        runtime = StreamsRuntime(None, lambda _message: None, "h1")
        runtime.attach(identity_reader=lambda: snapshot(relations(120)),
                       identity_contract=FLEET_IDENTITY_FACTS,
                       identity_authority_id=FLEET_AUTHORITY,
                       identity_namespace="harmony:node")

        async def publish(contract, subject, status, body):
            sent.append((contract, subject, status, body))
            return {"shadowed": False}
        runtime.publish_state = publish
        await runtime._identity_tick()
        self.assertGreater(len(sent), 1)
        self.assertEqual([row[1] for row in sent], [{"page": i} for i in range(len(sent))])
        self.assertTrue(all(row[0] == FLEET_IDENTITY_FACTS and row[2] == "ok" for row in sent))
        self.assertTrue(all(row[3]["page_index"] == index and
                            row[3]["page_count"] == len(sent)
                            for index, row in enumerate(sent)))

    async def test_shadow_registration_ack_is_not_reported_as_serving(self):
        runtime = StreamsRuntime(None, lambda _message: None, "h1")
        runtime.attach(identity_reader=lambda: snapshot(relations(1)),
                       identity_contract=FLEET_IDENTITY_FACTS,
                       identity_authority_id=FLEET_AUTHORITY,
                       identity_namespace="harmony:node")

        async def publish(*_args):
            return {"shadowed": True}
        runtime.publish_state = publish
        with self.assertRaisesRegex(RuntimeError, "identity_fact_authority_refused:shadowed"):
            await runtime._identity_tick()

    async def test_workload_identity_contract_uses_native_ingress_pages(self):
        path = os.path.join(self.temp.name, "identity.sock")
        fake = FakeIngress(path)
        await fake.start()
        logs = []
        runtime = StreamsRuntime(path, logs.append, "host-a")
        runtime.attach(identity_reader=lambda: snapshot(
            relations(1000, namespace="livestack:worker"), WORKLOAD_AUTHORITY),
            identity_contract=WORKLOAD_IDENTITY_FACTS,
            identity_authority_id=WORKLOAD_AUTHORITY,
            identity_namespace="livestack:worker")
        runtime.start()
        try:
            self.assertTrue(await fake.settle(
                lambda: fake.publishes and len(fake.publishes) == fake.publishes[0]["body"]["page_count"]), logs)
            self.assertGreater(len(fake.publishes), 1)
            self.assertEqual(fake.registers, [WORKLOAD_IDENTITY_FACTS])
            self.assertEqual([row["subject"] for row in fake.publishes],
                             [{"page": i} for i in range(len(fake.publishes))])
            self.assertTrue(all(set(row["body"]) == {
                "v", "generation", "sequence", "observed_at_ms", "page_index", "page_count", "facts"
            } for row in fake.publishes))
        finally:
            await runtime.stop()
            await fake.stop()

    async def test_fleet_source_publishes_only_from_its_cut_without_planning(self):
        path = os.path.join(self.temp.name, "fleet-identity.sock")
        fake = FakeIngress(path)
        await fake.start()

        class Broker:
            def __init__(self):
                self.planned = 0
                self.views = 0

            def fleet_view(self):
                self.views += 1
                return {}

            @staticmethod
            def identity_relations():
                relation = relations(1)[0]
                relation["observed_at_ms"] = int(time.time() * 1000) - 100
                return [relation], {"status": "ok", "published": 1}

        broker = Broker()
        publisher = IdentitySnapshotPublisher("livestack", FLEET_AUTHORITY)
        logs = []
        runtime = StreamsRuntime(path, logs.append, "host-a")
        runtime.attach(identity_reader=lambda: fleet_identity_cut(broker, publisher),
                       identity_contract=FLEET_IDENTITY_FACTS,
                       identity_authority_id=FLEET_AUTHORITY,
                       identity_namespace="harmony:node")
        runtime.start()
        try:
            self.assertTrue(await fake.settle(lambda: fake.publishes), logs)
            self.assertEqual(fake.registers, [FLEET_IDENTITY_FACTS])
            [publication] = fake.publishes
            self.assertEqual(publication["subject"], {"page": 0})
            self.assertEqual(publication["body"]["facts"][0]["subject"]["from"],
                             "harmony:node:node-0-")
            self.assertEqual((broker.views, broker.planned), (1, 0))
        finally:
            await runtime.stop()
            await fake.stop()

    async def test_fleet_authority_shadow_is_visible_and_not_treated_as_serving(self):
        path = os.path.join(self.temp.name, "fleet-shadow.sock")
        fake = FakeIngress(path, shadow={FLEET_IDENTITY_FACTS})
        await fake.start()
        logs = []
        runtime = StreamsRuntime(path, logs.append, "host-a")
        runtime.attach(identity_reader=lambda: snapshot(relations(1)),
                       identity_contract=FLEET_IDENTITY_FACTS,
                       identity_authority_id=FLEET_AUTHORITY,
                       identity_namespace="harmony:node")
        runtime.start()
        try:
            self.assertTrue(await fake.settle(lambda: fake.publishes), logs)
            for _ in range(20):
                if any("identity_fact_authority_refused:shadowed" in line for line in logs):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(any("identity_fact_authority_refused:shadowed" in line for line in logs), logs)
        finally:
            await runtime.stop()
            await fake.stop()

    async def test_workload_stream_reads_only_principal_mapped_worker_facts(self):
        path = os.path.join(self.temp.name, "workload.sqlite")
        class DecisionLedger:
            def __init__(self):
                self.records = []

            def append(self, record):
                self.records.append(record)
                return True

        ledger = DecisionLedger()
        store = WorkloadStore(path, handlers={"test.v1"}, decision_ledger=ledger)
        report = {"capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
                  "handlers": ["test.v1"], "ready": True}
        principals = [
            Principal("admin", "a" * 40, "admin", ("test.v1",)),
            Principal("mapped", "w" * 40, "worker", worker="worker-a",
                      host="physical-a", benchday_host_id="host-a"),
            Principal("unmapped", "u" * 40, "worker", worker="worker-b",
                      host="physical-b"),
        ]
        server = WorkloadServer(("127.0.0.1", 0), store, principals,
                                identity_authority_id=WORKLOAD_AUTHORITY)
        store.register("worker-a", "physical-a", "boot-a", report)
        store.register("worker-b", "physical-b", "boot-b", report)
        socket_path = os.path.join(self.temp.name, "workload-identity.sock")
        fake = FakeIngress(socket_path)
        await fake.start()
        logs = []
        runtime = StreamsRuntime(socket_path, logs.append, "host-a")
        runtime.attach(identity_reader=server.identity_facts,
                       identity_contract=WORKLOAD_IDENTITY_FACTS,
                       identity_authority_id=WORKLOAD_AUTHORITY,
                       identity_namespace="livestack:worker")
        runtime.start()
        try:
            self.assertTrue(await fake.settle(lambda: fake.publishes), logs)
            self.assertEqual(fake.registers, [WORKLOAD_IDENTITY_FACTS])
            facts = [fact for row in fake.publishes for fact in row["body"]["facts"]]
            self.assertEqual(len(facts), 1, facts)
            self.assertEqual(facts[0]["subject"]["from"], "livestack:worker:worker-a")
            self.assertEqual(facts[0]["subject"]["to"], "benchday:host:host-a")
            self.assertEqual(facts[0]["authority"], {"kind": "livestack", "id": WORKLOAD_AUTHORITY})
            self.assertEqual(ledger.records, [])
            self.assertEqual(store.list_jobs("admin"), [])
        finally:
            await runtime.stop()
            await fake.stop()
            server.server_close()

    async def test_workload_stream_omits_ambiguous_mapping_as_one_empty_page(self):
        path = os.path.join(self.temp.name, "ambiguous-workload.sqlite")
        store = WorkloadStore(path, handlers={"test.v1"})
        report = {"capacity": {"cpu": 2}, "available": {"cpu": 2}, "labels": {},
                  "handlers": ["test.v1"], "ready": True}
        principals = [
            Principal("admin", "a" * 40, "admin", ("test.v1",)),
            Principal("mapped-a", "b" * 40, "worker", worker="worker-a",
                      host="physical-a", benchday_host_id="host-a"),
            Principal("mapped-b", "c" * 40, "worker", worker="worker-a",
                      host="physical-b", benchday_host_id="host-b"),
        ]
        server = WorkloadServer(("127.0.0.1", 0), store, principals,
                                identity_authority_id=WORKLOAD_AUTHORITY)
        store.register("worker-a", "physical-a", "boot-a", report)
        socket_path = os.path.join(self.temp.name, "ambiguous-identity.sock")
        fake = FakeIngress(socket_path)
        await fake.start()
        runtime = StreamsRuntime(socket_path, lambda _message: None, "host-a")
        runtime.attach(identity_reader=server.identity_facts,
                       identity_contract=WORKLOAD_IDENTITY_FACTS,
                       identity_authority_id=WORKLOAD_AUTHORITY,
                       identity_namespace="livestack:worker")
        runtime.start()
        try:
            self.assertTrue(await fake.settle(lambda: fake.publishes), [])
            [publication] = fake.publishes
            self.assertEqual(publication["subject"], {"page": 0})
            self.assertEqual(publication["body"]["page_count"], 1)
            self.assertEqual(publication["body"]["facts"], [])
        finally:
            await runtime.stop()
            await fake.stop()
            server.server_close()

    async def test_threaded_stream_runtime_stops_and_joins_cleanly(self):
        from livestack_node import hostd_streams

        path = os.path.join(self.temp.name, "stop.sock")
        fake = FakeIngress(path)
        await fake.start()
        runtime = StreamsRuntime(path, lambda _message: None, "host-a")
        hostd_streams._run_in_thread(runtime, runtime.log)
        try:
            connected = False
            for _ in range(100):
                producer = runtime.producer
                if producer is not None and producer.client is not None and producer.client.is_open:
                    connected = True
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(connected)
            hostd_streams.stop_streams_runtime(runtime)
            self.assertFalse(runtime._thread.is_alive())
        finally:
            hostd_streams.stop_streams_runtime(runtime)
            await fake.stop()

    def test_runtime_registers_only_the_attached_identity_authority(self):
        runtime = StreamsRuntime(None, lambda _message: None, "h1")
        runtime.attach(identity_reader=lambda: {}, identity_contract=WORKLOAD_IDENTITY_FACTS,
                       identity_authority_id=WORKLOAD_AUTHORITY,
                       identity_namespace="livestack:worker")
        register, _serve = runtime.contracts()
        self.assertEqual(register, [WORKLOAD_IDENTITY_FACTS])

    def test_fleet_identity_contract_is_attached_only_to_observe_broker(self):
        from livestack_node import hostd_streams

        class Broker:
            host_id = "host-a"
            fleet_demand = None
            policy_runtime = None

            @staticmethod
            def fleet_view():
                return {}

            @staticmethod
            def membership_snapshot():
                return {}

        runtimes = []
        logs = []
        with patch.dict(os.environ, {"HARMONY_STREAMS": "1",
                                     "HARMONY_STREAMS_DIR": self.temp.name}):
            with patch.object(hostd_streams, "_run_in_thread",
                              side_effect=lambda runtime, _log: runtimes.append(runtime)):
                publisher = IdentitySnapshotPublisher("livestack", FLEET_AUTHORITY)
                hostd_streams.start_for_hostd(Broker(), "host-a", True, logs.append,
                                              identity_publisher=publisher)
                hostd_streams.start_for_hostd(Broker(), "host-b", False, logs.append,
                                              identity_publisher=publisher)
        fleet_register, fleet_serve = runtimes[0].contracts()
        host_register, _host_serve = runtimes[1].contracts()
        self.assertEqual(fleet_register, [HOST, FLEET, FLEET_IDENTITY_FACTS])
        self.assertEqual(sorted(fleet_serve), [REQUEST])
        self.assertEqual(host_register, [HOST])
        self.assertNotIn(FLEET_IDENTITY_FACTS, host_register)

    def test_workload_authority_requires_explicit_identity_configuration(self):
        from livestack_node import hostd_streams

        runtimes = []
        logs = []
        with patch.object(hostd_streams, "enabled", return_value=True):
            with patch.object(hostd_streams, "_run_in_thread",
                              side_effect=lambda runtime, _log: runtimes.append(runtime)):
                configured = hostd_streams.start_for_workload_authority(
                    object(), "host-a", os.path.join(self.temp.name, "configured"),
                    log=logs.append, identity_reader=lambda: {},
                    identity_authority_id=WORKLOAD_AUTHORITY)
                unconfigured = hostd_streams.start_for_workload_authority(
                    object(), "host-a", os.path.join(self.temp.name, "unconfigured"),
                    log=logs.append, identity_reader=lambda: {},
                    identity_authority_id=None)
        configured_register, configured_serve = configured.contracts()
        unconfigured_register, _unconfigured_serve = unconfigured.contracts()
        self.assertEqual(configured_register, [WORKLOAD_IDENTITY_FACTS])
        self.assertEqual(sorted(configured_serve), ["harmony.workload/1"])
        self.assertEqual(unconfigured_register, [])
        self.assertTrue(any("identity_authority_not_configured" in line for line in logs))
        for runtime in runtimes:
            runtime.workload.ledger.close()

    def test_identity_refresh_is_fifteen_seconds(self):
        self.assertEqual(IDENTITY_CUT_REFRESH_S, 15.0)


if __name__ == "__main__":
    unittest.main()
