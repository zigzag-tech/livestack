import time

import pytest

from livestack_node.identity_facts import (
    IdentitySnapshotPublisher,
    hosted_on_fact,
)
from livestack_node.hostbroker import HostBroker



def test_identity_snapshot_is_bounded_fenced_and_uses_explicit_relation():
    publisher = IdentitySnapshotPublisher("livestack", "fleet-a")
    relation = {
        "resource_namespace": "harmony:node",
        "resource_id": "node-a",
        "host_id": "host-a",
        "observed_at_ms": 1_800_000_000_000,
        "ttl_ms": 30_000,
    }
    first = publisher.snapshot([relation], now_ms=1_800_000_000_100)
    second = publisher.snapshot([relation], now_ms=1_800_000_000_200)

    assert first["generation"] == second["generation"]
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    [fact] = first["facts"]
    assert fact["v"] == 1
    assert fact["subject"] == {
        "kind": "edge",
        "from": "harmony:node:node-a",
        "edge_type": "hosted_on",
        "to": "benchday:host:host-a",
    }
    assert fact["authority"] == {"kind": "livestack", "id": "fleet-a"}
    assert fact["observed_at_ms"] == relation["observed_at_ms"]
    assert fact["ttl_ms"] == relation["ttl_ms"]
    assert fact["fence"] == {"generation": first["generation"], "sequence": 1}
    assert fact["scope"] == {"kind": "owner_account"}


def test_identity_facts_refuse_invalid_ids_and_unbounded_ttl():
    with pytest.raises(ValueError, match="invalid_identity"):
        hosted_on_fact(
            authority_kind="livestack",
            authority_id="fleet-a",
            resource_namespace="harmony:node",
            resource_id="",
            host_id="host-a",
            generation="g",
            sequence=1,
            observed_at_ms=1_800_000_000_000,
        )
    with pytest.raises(ValueError, match="invalid_ttl"):
        hosted_on_fact(
            authority_kind="livestack",
            authority_id="fleet-a",
            resource_namespace="harmony:node",
            resource_id="node-a",
            host_id="host-a",
            generation="g",
            sequence=1,
            observed_at_ms=1_800_000_000_000,
            ttl_ms=600_001,
        )


def test_identity_facts_reject_invalid_unicode_ids():
    with pytest.raises(ValueError, match="invalid_identity"):
        hosted_on_fact(
            authority_kind="livestack",
            authority_id="fleet-a",
            resource_namespace="harmony:node",
            resource_id=chr(0xD800),
            host_id="host-a",
            generation="g",
            sequence=1,
            observed_at_ms=1_800_000_000_000,
        )


def test_hostbroker_identity_relations_require_recent_explicit_rows():
    broker = HostBroker(peers=[])
    now = int(time.time() * 1000)
    broker._capabilities = {
        "fresh": {"identity_id": "node-a", "benchday_host_id": "host-a"},
        "missing": {"identity_id": "node-b"},
        "stale": {"identity_id": "node-c", "benchday_host_id": "host-c"},
    }
    broker._capability_observed_at_ms = {
        "fresh": now,
        "missing": now,
        "stale": now - 60_001,
    }

    relations, counts = broker.identity_relations()
    assert [(r["resource_id"], r["host_id"]) for r in relations] == [("node-a", "host-a")]
    assert counts["published"] == 1
    assert counts["missing_benchday_host"] == 1
    assert counts["stale_observation"] == 1


def test_hostbroker_omits_conflicting_host_observations():
    broker = HostBroker(peers=[])
    now = int(time.time() * 1000)
    broker._capabilities = {
        "a": {"identity_id": "node-a", "benchday_host_id": "host-a"},
        "b": {"identity_id": "node-a", "benchday_host_id": "host-b"},
    }
    broker._capability_observed_at_ms = {"a": now, "b": now}

    relations, counts = broker.identity_relations()
    assert relations == []
    assert counts["conflicting_observation"] == 1
def test_hostbroker_keeps_a_conflicted_identity_omitted_after_later_rows():
    broker = HostBroker(peers=[])
    now = int(time.time() * 1000)
    broker._capabilities = {
        "a": {"identity_id": "node-a", "benchday_host_id": "host-a"},
        "b": {"identity_id": "node-a", "benchday_host_id": "host-b"},
        "c": {"identity_id": "node-a", "benchday_host_id": "host-c"},
    }
    broker._capability_observed_at_ms = {"a": now, "b": now, "c": now}

    relations, counts = broker.identity_relations()
    assert relations == []
    assert counts["conflicting_observation"] == 1
