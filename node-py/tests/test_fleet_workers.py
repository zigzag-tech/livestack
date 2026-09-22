"""Tests for the reusable-worker seam: the runner's failure classification, the
correlation token, and the Aliyun adapter's pure parts.

The bill is the assertion. `FakeWorkerProvider.created` is the number of
machines that would have been rented, and every scenario here is one of the ways
a control plane rents two.
"""
import base64
import urllib.error

import pytest

from fake_worker_provider import FakeWorkerProvider
from livestack_node.fleet_operations import (
    ANNOUNCED, CREATED, CREATING, REJECTED, UNCERTAIN, OperationStore,
)
from livestack_node.fleet_workers import (
    AliyunEcsWorkerProvider, LookupUnavailable, RequestRejected, UncertainEffect,
    WorkerSpec, announce_from_view, bootstrap_script, percent_encode,
    recover_all, reconcile, run_provision, sign_rpc, string_to_sign,
)
from livestack_node.provision import CapacityError


SPEC = WorkerSpec(region="cn-heyuan", instance_type="ecs.g8i.2xlarge",
                  announce_env={"LIVESTACK_BROKER_URL": "http://broker:8801"})


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def store(tmp_path, clock=None, **kw):
    return OperationStore(str(tmp_path / "ops.sqlite3"),
                          clock=clock or Clock(), **kw)


def claimed(st, key="key-1", **kw):
    return st.claim(job_id="j1", owner="acct_a", target_id="pool-a", kind="llm",
                    idempotency_key=key, provider="fake", **kw)


# --- what the runner does with each kind of failure -------------------------
def test_a_good_create_is_recorded_with_the_instance_id(tmp_path):
    st = store(tmp_path)
    p = FakeWorkerProvider()
    op = run_provision(st, claimed(st), p, SPEC)
    assert op.state == CREATED and op.provider_instance_id == "fake-i-1"
    assert len(p.created) == 1


def test_no_capacity_is_rejected_and_classified_as_a_shortage(tmp_path):
    st = store(tmp_path)
    op = run_provision(st, claimed(st), FakeWorkerProvider(no_capacity=True), SPEC)
    assert op.state == REJECTED
    assert op.error["class"] == "capacity_shortage"


def test_a_refused_request_is_rejected_and_classified_as_our_fault(tmp_path):
    st = store(tmp_path)
    op = run_provision(st, claimed(st), FakeWorkerProvider(refuse=True), SPEC)
    assert op.state == REJECTED
    assert op.error["class"] == "request_or_workload_fault"


def test_a_lost_reply_is_uncertain_and_never_retried(tmp_path):
    st = store(tmp_path)
    p = FakeWorkerProvider(lose_reply=True)
    op = run_provision(st, claimed(st), p, SPEC)
    assert op.state == UNCERTAIN and op.error["class"] == "uncertain_effect"
    # The machine exists. The ONLY path out is reconcile, and it bills nothing.
    out = reconcile(st, op.operation_id, p)
    assert out.state == CREATED and out.provider_instance_id == "fake-i-1"
    assert len(p.created) == 1 and len(p.create_calls) == 1


def test_an_unmodelled_failure_resolves_to_uncertain_not_to_rejected(tmp_path):
    """The expensive direction of the mistake. An unknown failure that read as
    'nothing happened' would free the quota and invite a second create."""
    st = store(tmp_path)

    class Weird(FakeWorkerProvider):
        def create(self, **kw):
            raise ZeroDivisionError("something nobody modelled")

    op = run_provision(st, claimed(st), Weird(), SPEC)
    assert op.state == UNCERTAIN and op.error["code"] == "ZeroDivisionError"


def test_a_late_completion_after_a_lost_reply_still_bills_once(tmp_path):
    st = store(tmp_path)
    p = FakeWorkerProvider(lose_reply=True, late_completion=True)
    op = run_provision(st, claimed(st), p, SPEC)
    assert op.state == UNCERTAIN and p.created == {}
    p.settle()                                   # the create lands, late
    out = reconcile(st, op.operation_id, p)
    assert out.state == CREATED
    assert len(p.created) == 1 and len(p.create_calls) == 1


def test_restart_reconciles_before_any_new_create_is_possible(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock)
    p = FakeWorkerProvider(lose_reply=True)
    op = run_provision(st, claimed(st), p, SPEC)
    assert op.state == UNCERTAIN

    # A new process over the same file.
    st2 = store(tmp_path, clock)
    out = recover_all(st2, {"fake": p})
    assert [o.state for o in out] == [CREATED]
    assert len(p.create_calls) == 1


def test_a_provider_that_cannot_be_asked_leaves_the_operation_uncertain(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock)
    p = FakeWorkerProvider(lose_reply=True)
    run_provision(st, claimed(st), p, SPEC)
    p.lookup_down = True
    st2 = store(tmp_path, clock)
    assert recover_all(st2, {"fake": p}) == []
    assert st2.active()[0].state == UNCERTAIN     # still holding its quota


def test_run_provision_refuses_an_operation_that_is_not_creating(tmp_path):
    st = store(tmp_path)
    op = claimed(st)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    with pytest.raises(ValueError):
        run_provision(st, st.get(op.operation_id), FakeWorkerProvider(), SPEC)


# --- the correlation token --------------------------------------------------
def test_the_operation_id_reaches_the_instance_boot_environment(tmp_path):
    st = store(tmp_path)
    seen = {}

    class Capture(FakeWorkerProvider):
        def create(self, *, operation_id, idempotency_key, spec):
            seen["env"] = dict(spec.announce_env)
            seen["script"] = bootstrap_script(spec)
            return super().create(operation_id=operation_id,
                                  idempotency_key=idempotency_key, spec=spec)

    op = run_provision(st, claimed(st), Capture(), SPEC)
    assert seen["env"]["LIVESTACK_OPERATION_ID"] == op.operation_id
    assert f"export LIVESTACK_OPERATION_ID='{op.operation_id}'" in seen["script"]
    # The caller's own env survives beside it.
    assert seen["env"]["LIVESTACK_BROKER_URL"] == "http://broker:8801"


def view(nodes):
    return {"hosts": {"h1": {"nodes": nodes}}}


def test_only_a_node_carrying_this_operation_id_and_ready_greens_it(tmp_path):
    st = store(tmp_path)
    op = run_provision(st, claimed(st), FakeWorkerProvider(), SPEC)

    # A fresh, ready node that is nothing to do with us.
    assert announce_from_view(st, view([{"peer": "http://other", "ready": True}])) == []
    # Ours, but not serving yet: being billed is not being usable.
    assert announce_from_view(st, view([
        {"peer": "http://mine", "ready": False,
         "operation_id": op.operation_id}])) == []
    assert st.get(op.operation_id).state == CREATED

    greened = announce_from_view(st, view([
        {"peer": "http://mine", "ready": True,
         "operation_id": op.operation_id}]))
    assert [o.state for o in greened] == [ANNOUNCED]


# --- the Aliyun adapter's pure parts ----------------------------------------
def test_the_string_to_sign_is_built_exactly_as_the_spec_states():
    """Assert the STRING, not the digest. A digest mismatch is one opaque
    symptom for three different mistakes — ordering, encoding, the `&`-suffixed
    secret — and Aliyun reports all three as `SignatureDoesNotMatch`."""
    assert string_to_sign({"b": "2", "a": "1 2"}, "GET") == \
        "GET&%2F&a%3D1%25202%26b%3D2"


def test_percent_encoding_is_rfc3986_and_not_encodeURIComponent():
    """`encodeURIComponent` leaves `!'()` literal; the signature spec does not.
    A value containing one of them signs differently under the two, and the only
    report you get is `SignatureDoesNotMatch`."""
    assert percent_encode("a b") == "a%20b"
    assert percent_encode("a*b") == "a%2Ab"
    assert percent_encode("a~b") == "a~b"
    assert percent_encode("it's (x)!") == "it%27s%20%28x%29%21"


def test_the_signature_is_stable_and_order_independent():
    a = sign_rpc({"A": "a b", "B": "a*b", "C": "a~b"}, access_key_secret="s")
    b = sign_rpc({"C": "a~b", "B": "a*b", "A": "a b"}, access_key_secret="s")
    assert a["Signature"] == b["Signature"]
    # A different secret must move it, or the secret is not reaching the HMAC.
    assert sign_rpc({"A": "1"}, access_key_secret="s")["Signature"] != \
        sign_rpc({"A": "1"}, access_key_secret="t")["Signature"]


def test_a_4xx_is_a_refused_request_and_a_5xx_is_an_unknown_effect():
    """The classification that decides whether an operation is `rejected` (safe,
    frees the quota) or `uncertain` (must be reconciled). Getting it backwards on
    a 5xx is exactly how a second machine gets rented."""
    def transport(status, body):
        def go(_endpoint, _body):
            raise urllib.error.HTTPError(
                "u", status, "err", {}, __import__("io").BytesIO(body.encode()))
        return go

    def provider(status, body):
        return AliyunEcsWorkerProvider(
            access_key_id="k", access_key_secret="s",
            transport=transport(status, body))

    with pytest.raises(RequestRejected):
        provider(400, '{"Code":"InvalidImageId.NotFound"}').call("RunInstances", {})
    with pytest.raises(CapacityError):
        provider(403, '{"Code":"OperationDenied.NoStock"}').call("RunInstances", {})
    with pytest.raises(UncertainEffect):
        provider(503, '{"Code":"ServiceUnavailable"}').call("RunInstances", {})


def test_a_timeout_is_an_unknown_effect_never_a_rejection():
    def transport(_endpoint, _body):
        raise TimeoutError("read timed out")

    with pytest.raises(UncertainEffect):
        AliyunEcsWorkerProvider(access_key_id="k", access_key_secret="s",
                                transport=transport).call("RunInstances", {})


def test_find_that_cannot_reach_the_provider_raises_rather_than_answering_no():
    def transport(_endpoint, _body):
        raise TimeoutError("read timed out")

    with pytest.raises(LookupUnavailable):
        AliyunEcsWorkerProvider(access_key_id="k", access_key_secret="s",
                                transport=transport).find("key-1")


def test_create_sends_the_idempotency_key_as_a_client_token_and_a_tag():
    sent = {}

    def transport(_endpoint, body):
        import urllib.parse
        sent.update(dict(urllib.parse.parse_qsl(body.decode())))
        return '{"InstanceIdSets":{"InstanceIdSet":["i-abc"]}}'

    p = AliyunEcsWorkerProvider(access_key_id="k", access_key_secret="s",
                                transport=transport, region="cn-heyuan")
    iid = p.create(operation_id="OP123", idempotency_key="key-1", spec=SPEC)
    assert iid == "i-abc"
    assert sent["ClientToken"] == "key-1"
    tags = {sent[f"Tag.{i}.Key"]: sent[f"Tag.{i}.Value"]
            for i in range(1, 9) if f"Tag.{i}.Key" in sent}
    assert tags["livestack:operation"] == "OP123"
    assert tags["livestack:key"] == "key-1"
    assert "LIVESTACK_BROKER_URL" in base64.b64decode(sent["UserData"]).decode()


def test_a_200_with_no_instance_id_is_uncertain_not_success():
    def transport(_endpoint, _body):
        return '{"RequestId":"r1"}'

    p = AliyunEcsWorkerProvider(access_key_id="k", access_key_secret="s",
                                transport=transport)
    with pytest.raises(UncertainEffect):
        p.create(operation_id="OP", idempotency_key="key-1", spec=SPEC)


def test_a_missing_credential_is_a_refused_request():
    p = AliyunEcsWorkerProvider(access_key_id="", access_key_secret="")
    with pytest.raises(RequestRejected):
        p.call("DescribeInstances", {})


# --- the two failures that outlive the request ------------------------------
def test_a_cleanup_failure_leaves_the_instance_id_on_record(tmp_path):
    """Released-but-still-running is money burning with nobody watching. The
    release still applies — a node that is empty must come back — but the
    provider id survives in the record, because reaping it by hand is the only
    remaining move and it needs the id."""
    st = store(tmp_path)
    p = FakeWorkerProvider()
    op = run_provision(st, claimed(st), p, SPEC)
    st.announce(op.operation_id, ready=True, node="http://burst-1")

    def boom(_iid):
        raise RuntimeError("provider API 500")

    p.terminate = boom
    released = st.release(op.operation_id, busy=lambda: None)
    try:
        p.terminate(released.provider_instance_id)
    except RuntimeError:
        pass
    assert released.state == "released"
    assert st.get(op.operation_id).provider_instance_id == "fake-i-1"


def test_a_node_falling_out_of_the_view_does_not_change_the_operation(tmp_path):
    """Stale membership. A node that has gone MIA is not evidence that anything
    happened to its operation — it is evidence that we cannot see it, and
    "cannot see" must never read as either success or release."""
    st = store(tmp_path)
    op = run_provision(st, claimed(st), FakeWorkerProvider(), SPEC)
    announce_from_view(st, view([{"peer": "http://burst-1", "ready": True,
                                  "operation_id": op.operation_id}]))
    assert st.get(op.operation_id).state == ANNOUNCED

    # The view no longer lists it at all.
    assert announce_from_view(st, view([])) == []
    assert st.get(op.operation_id).state == ANNOUNCED
    # And a node that reappears carrying a RELEASED operation's id greens nothing.
    st.release(op.operation_id, busy=lambda: None)
    assert announce_from_view(st, view([{"peer": "http://burst-1", "ready": True,
                                         "operation_id": op.operation_id}])) == []
    assert st.get(op.operation_id).state == "released"
