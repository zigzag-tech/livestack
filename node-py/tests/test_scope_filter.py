"""Scope is a grant the fleet reads on admission.

A node may announce `scope: {"kind": "owner"|"org"|"realm", "id": ...}` — who
it is pooled for. The admission path rejects a target whose scope does not
admit the request's owner, naming the scope, so a self-scoped GPU is never
silently spent on another account. Matching is by namespace: `sorbonne`
admits `sorbonne` and `sorbonne:acct_1`, not `attune:acct_a`.
"""
from livestack_node.announce import _scope_admits, node_scope
from livestack_node.fleet_admit import admit, targets_from_view

SELF_SCOPED_VIEW = {
    "generated_at": 1000.0,
    "hosts": {
        "gpu-box": {"nodes": [{
            "peer": "http://100.64.0.9:8100/livestack", "state": "fresh",
            "ready": True, "kinds": ["llm"], "detail": "resident",
            "unseen_seconds": 0.0, "load": {"in_flight": 0},
            "scope": {"kind": "owner", "id": "sorbonne"},
            "units": [{"kind": "llm", "resident": True}]}]},
    },
}


def test_a_self_scoped_node_is_excluded_for_another_owner():
    r = admit(SELF_SCOPED_VIEW, kind="llm", owner="attune:acct_a", now=1000.0)
    assert r["granted"] is False
    scoped = [c for c in r["candidates"]
              if "scoped to" in (c.reason or "")]
    assert len(scoped) == 1
    assert "filtered: scoped to owner sorbonne" in scoped[0].reason


def test_a_self_scoped_node_is_included_for_its_own_owner():
    for owner in ("sorbonne", "sorbonne:acct_1"):
        r = admit(SELF_SCOPED_VIEW, kind="llm", owner=owner, now=1000.0)
        assert r["granted"] is True, f"{owner}: its own scope admits it"
        assert r["target"]["host_id"] == "gpu-box"


def test_an_unscoped_node_stays_pooled_for_everyone():
    view = {"generated_at": 1000.0,
            "hosts": {"h": {"nodes": [{
                "peer": "http://h:8100/livestack", "state": "fresh",
                "ready": True, "kinds": ["llm"], "unseen_seconds": 0.0,
                "load": {"in_flight": 0}}]}}}
    for owner in ("attune:acct_a", "sorbonne", "consumer"):
        r = admit(view, kind="llm", owner=owner, now=1000.0)
        assert r["granted"] is True, "absent scope is the fleet default"


def test_scope_matching_is_by_namespace():
    scope = {"kind": "owner", "id": "sorbonne"}
    assert _scope_admits(scope, "sorbonne")
    assert _scope_admits(scope, "sorbonne:acct_1")
    assert not _scope_admits(scope, "sorbonne-miami")   # prefix is not ownership
    assert not _scope_admits(scope, "attune:acct_a")
    assert _scope_admits(None, "anyone")
    assert _scope_admits({}, "anyone")
    assert _scope_admits({"id": ""}, "anyone")          # malformed = no scope


# -- the grant travels: env -> announce -> roster -> snapshot -> view ---------

def test_node_scope_parses_the_operator_grant(monkeypatch):
    monkeypatch.delenv("LIVESTACK_NODE_SCOPE", raising=False)
    assert node_scope() is None
    monkeypatch.setenv("LIVESTACK_NODE_SCOPE",
                       '{"kind": "owner", "id": "sorbonne"}')
    assert node_scope() == {"kind": "owner", "id": "sorbonne"}
    # A typo must not half-scope a node.
    monkeypatch.setenv("LIVESTACK_NODE_SCOPE", "{not json")
    assert node_scope() is None
    monkeypatch.setenv("LIVESTACK_NODE_SCOPE", '{"kind": "nope", "id": "x"}')
    assert node_scope() is None
    monkeypatch.setenv("LIVESTACK_NODE_SCOPE", '{"kind": "owner"}')
    assert node_scope() is None


def test_the_announce_carries_scope_into_the_roster_row():
    from livestack_node.hostbroker import HostBroker

    broker = HostBroker(devices=[], peers=[], clock=lambda: 1000.0)
    url = "http://100.64.0.9:8100/livestack"
    # Register directly through the broker, as POST /peers does with the
    # announced payload.
    broker.register_url(
        url,
        make_peer=lambda u: None,
        host_id="gpu-box", kinds=["llm"],
        scope={"kind": "owner", "id": "sorbonne"})
    row = next(r for r in broker.membership_snapshot() if r["peer"] == url)
    assert row["scope"] == {"kind": "owner", "id": "sorbonne"}


def test_targets_from_view_receives_the_scope_from_the_view_row():
    targets, rejected = targets_from_view(SELF_SCOPED_VIEW, "llm",
                                          owner="attune:acct_a")
    assert targets == ()
    assert any("scoped to owner sorbonne" in r.reason for r in rejected)
    targets, _ = targets_from_view(SELF_SCOPED_VIEW, "llm", owner="sorbonne")
    assert len(targets) == 1
