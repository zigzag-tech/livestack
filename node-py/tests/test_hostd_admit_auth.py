"""hostd POST /admit grows a principal: the write endpoints agree on who is asking.

The requirement's scenarios, in the small, against the real FastAPI app:

* a fixed principal naming another owner is REFUSED (403 naming the mismatch)
  and nothing is recorded against either owner — nothing was spent;
* a delegating principal outside its prefix is REFUSED (403) and the refusal
  is ledgered under the principal's name — a known caller reaching across
  tenants is a fact a retrospective needs;
* no credential at all is 401, with nothing planned and nothing ledgered.

And the deploy that configures nothing sees byte-for-byte today's behaviour:
owner from the body, default "consumer", an Authorization header ignored.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.fleet_auth import load_principals  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402

TOK_ATTUNE = "a" * 40
TOK_BENCHDAY = "b" * 40

# The requirement's cast: `attune` is one service with one identity;
# benchday's principal speaks for accounts under its prefix.
PRINCIPALS = load_principals(
    '{"%s": {"name": "attune", "owner": "attune"},'
    ' "%s": {"name": "benchday-hub", "delegate_prefix": "benchday:"}}'
    % (TOK_ATTUNE, TOK_BENCHDAY))


class _Plan:
    """The slice of a plan the /admit handler touches: .of(cls) and .summary()."""
    def __init__(self, actions=()):
        self.actions = list(actions)

    def of(self, cls):
        return [a for a in self.actions if isinstance(a, cls)]

    def summary(self):
        return {"actions": len(self.actions)}


class _Broker:
    peers = []

    def __init__(self, principals):
        self.fleet_principals = principals
        self.planned = []        # Requests that reached the planner
        self.admit_records = []  # Everything emit_admit was asked to ledger

    def plan_and_apply(self, requests, _last_evicted_at):
        self.planned.extend(requests)
        return _Plan()

    def emit_admit(self, result, request, lease_id=None):
        self.admit_records.append({"result": dict(result),
                                   "request": dict(request)})


def _client(monkeypatch, principals):
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")
    broker = _Broker(principals)
    return TestClient(build_app(broker), raise_server_exceptions=False), broker


# -- the requirement's scenarios ----------------------------------------------

def test_a_fixed_principal_naming_another_owner_is_403_and_nothing_recorded(
        monkeypatch):
    client, broker = _client(monkeypatch, PRINCIPALS)
    r = client.post("/admit", json={"kind": "qwen", "owner": "media-corpus"},
                    headers={"Authorization": f"Bearer {TOK_ATTUNE}"})
    assert r.status_code == 403
    assert "cannot name 'media-corpus'" in r.json()["detail"]
    # Nothing reached the planner and NOTHING was ledgered against either
    # owner — a refused identity claim spends nothing and must not pollute
    # the record of the account it wrongly named.
    assert broker.planned == []
    assert broker.admit_records == []


def test_a_delegating_principal_outside_its_prefix_is_403_and_ledgered(
        monkeypatch):
    client, broker = _client(monkeypatch, PRINCIPALS)
    r = client.post("/admit", json={"kind": "qwen", "owner": "attune:acct_x"},
                    headers={"Authorization": f"Bearer {TOK_BENCHDAY}"})
    assert r.status_code == 403
    assert "benchday-hub" in r.json()["detail"]
    assert broker.planned == []
    # The refusal IS ledgered, under the PRINCIPAL's name — "who tried" is the
    # fact; the owner it attempted to spend as was never established.
    assert len(broker.admit_records) == 1
    rec = broker.admit_records[0]
    assert rec["request"]["principal"] == "benchday-hub"
    assert rec["request"]["owner"] == "attune:acct_x"
    assert rec["result"]["refused"] == "auth_prefix"


def test_no_credential_is_401_when_principals_are_configured(monkeypatch):
    client, broker = _client(monkeypatch, PRINCIPALS)
    assert client.post("/admit", json={"kind": "qwen"}).status_code == 401
    assert client.post(
        "/admit", json={"kind": "qwen"},
        headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert broker.planned == []
    assert broker.admit_records == []


def test_a_delegating_principal_inside_its_prefix_is_admitted(monkeypatch):
    client, broker = _client(monkeypatch, PRINCIPALS)
    r = client.post("/admit", json={"kind": "qwen", "owner": "benchday:acct_b"},
                    headers={"Authorization": f"Bearer {TOK_BENCHDAY}"})
    assert r.status_code == 200
    assert [q.owner for q in broker.planned] == ["benchday:acct_b"]


# -- the un-configured deploy changes not at all --------------------------------

def test_with_no_principals_the_body_owner_passes_through_unchanged(monkeypatch):
    """Byte-for-byte today's behaviour on the deploy that configured nothing:
    the owner is whatever the body says, defaulting to 'consumer'."""
    client, broker = _client(monkeypatch, None)
    r = client.post("/admit", json={"kind": "qwen", "owner": "media-corpus"})
    assert r.status_code == 200
    assert [q.owner for q in broker.planned] == ["media-corpus"]

    client2, broker2 = _client(monkeypatch, None)
    r2 = client2.post("/admit", json={"kind": "qwen"})
    assert r2.status_code == 200
    assert [q.owner for q in broker2.planned] == ["consumer"]


def test_with_no_principals_an_authorization_header_is_ignored(monkeypatch):
    """Auth off is total: a caller that already sends a token (e.g. an engine
    upgraded before its broker) must not change behaviour on a broker that
    has no principal table."""
    client, broker = _client(monkeypatch, None)
    r = client.post("/admit", json={"kind": "qwen", "owner": "media-corpus"},
                    headers={"Authorization": f"Bearer {TOK_ATTUNE}"})
    assert r.status_code == 200
    assert [q.owner for q in broker.planned] == ["media-corpus"]


def test_a_configured_but_empty_table_fails_closed(monkeypatch):
    """A token source that yielded nothing (refused world-readable file,
    malformed JSON) is an ALARM state, not "auth off": every caller 401s
    until the operator fixes it. An unauthenticated broker that discovered
    its config was broken must not keep the door open."""
    client, broker = _client(monkeypatch, {})
    r = client.post("/admit", json={"kind": "qwen", "owner": "media-corpus"})
    assert r.status_code == 401
    assert broker.planned == []


# -- read endpoints record the caller when a credential is present ------------
#
# Reads stay open — that is the requirement's "MAY" — but a valid credential
# is recorded on the ledger row beside the owner, so a retrospective can tell
# "the hub, looking" from "nobody, looking". /fleet, /peers and /plan emit no
# ledger rows today; /fleet/rank is the read that does.

class _RankBroker:
    peers = []
    fleet_principals = PRINCIPALS

    def __init__(self):
        self.records = []

    def fleet_view(self):
        return {"generated_at": 1000.0,
                "hosts": {"h": {"nodes": [
                    {"peer": "http://h:8100/livestack", "state": "fresh",
                     "ready": True, "kinds": ["qwen"], "detail": "resident",
                     "unseen_seconds": 0.0, "probe_ms": 2.0,
                     "units": [{"kind": "qwen", "resident": True}]}]}}}

    def emit_rank(self, result):
        self.records.append(result)


def _rank_client(monkeypatch):
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")
    broker = _RankBroker()
    return TestClient(build_app(broker), raise_server_exceptions=False), broker


def test_a_rank_with_a_token_records_the_principal(monkeypatch):
    client, broker = _rank_client(monkeypatch)
    body = client.get("/fleet/rank", params={"kind": "qwen"},
                      headers={"Authorization": f"Bearer {TOK_ATTUNE}"}).json()
    assert body["principal"] == "attune"
    assert len(broker.records) == 1
    assert broker.records[0]["principal"] == "attune"


def test_a_rank_without_a_token_records_null(monkeypatch):
    client, broker = _rank_client(monkeypatch)
    body = client.get("/fleet/rank", params={"kind": "qwen"}).json()
    assert body["principal"] is None
    assert broker.records[0]["principal"] is None
    # An invalid token on a READ is anonymous, never a refusal — the refusal
    # machinery belongs to the write endpoints.
    body2 = client.get("/fleet/rank", params={"kind": "qwen"},
                       headers={"Authorization": "Bearer wrong"}).json()
    assert body2["principal"] is None
