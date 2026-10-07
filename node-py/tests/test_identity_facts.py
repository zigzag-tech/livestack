import time

import pytest

from livestack_node.identity_facts import (
    IdentitySnapshotPublisher,
    hosted_on_fact,
)
from livestack_node.hostbroker import HostBroker

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.fleet_auth import load_principals  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402


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


class _IdentityBroker:
    def __init__(self, principals):
        self.fleet_principals = principals
        self.refreshed = 0
        self.planned = 0

    def fleet_view(self):
        self.refreshed += 1
        return {}

    def identity_relations(self):
        return ([{
            "resource_namespace": "harmony:node",
            "resource_id": "node-a",
            "host_id": "host-a",
            "observed_at_ms": 1_800_000_000_000,
            "ttl_ms": 60_000,
        }], {"status": "ok", "known_peers": 1, "published": 1})


def test_fleet_identity_endpoint_requires_auth_and_returns_complete_cut(monkeypatch):
    token = "f" * 40
    principals = load_principals(
        '{"%s": {"name": "benchday-hub", "delegate_prefix": "benchday:"}}' % token)
    broker = _IdentityBroker(principals)
    monkeypatch.setenv("LIVESTACK_IDENTITY_AUTHORITY_ID", "fleet-a")
    client = TestClient(build_app(broker), raise_server_exceptions=False)

    assert client.get("/fleet/identity-facts").status_code == 401
    assert client.get("/fleet/identity-facts",
                      headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.get("/fleet/identity-facts",
                          headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["v"] == 1
    assert body["sequence"] == 1
    assert body["source_state"]["published"] == 1
    [fact] = body["facts"]
    assert fact["authority"] == {"kind": "livestack", "id": "fleet-a"}
    assert fact["subject"]["from"] == "harmony:node:node-a"
    assert fact["subject"]["to"] == "benchday:host:host-a"
    assert broker.refreshed == 1
    assert broker.planned == 0


def test_fleet_identity_endpoint_fails_closed_without_source_auth(monkeypatch):
    monkeypatch.setenv("LIVESTACK_IDENTITY_AUTHORITY_ID", "fleet-a")
    broker = _IdentityBroker(None)
    client = TestClient(build_app(broker), raise_server_exceptions=False)
    response = client.get("/fleet/identity-facts",
                          headers={"Authorization": "Bearer arbitrary"})

    assert response.status_code == 503
    assert "identity_fact_auth_not_configured" in response.text


def test_fleet_identity_endpoint_fails_closed_without_authority_id(monkeypatch):
    monkeypatch.delenv("LIVESTACK_IDENTITY_AUTHORITY_ID", raising=False)
    broker = _IdentityBroker(load_principals(
        '{"%s": {"name": "benchday-hub", "delegate_prefix": "benchday:"}}' % ("f" * 40)))
    client = TestClient(build_app(broker), raise_server_exceptions=False)

    response = client.get("/fleet/identity-facts",
                          headers={"Authorization": "Bearer " + "f" * 40})
    assert response.status_code == 503
    assert "identity_authority_not_configured" in response.text

