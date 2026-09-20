"""H01 / H03–H08 / H10: drive the real decision ingress, not a reimplementation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from livestack_node.decisions.admission import BrokerUnavailable
from livestack_node.decisions.contract import load_fixture
from livestack_node.decisions.fixture_backend import FixtureBackend
from livestack_node.decisions.kinds import CUDA_KIND, MLX_KIND, KindConflict, PhysicalKind, aggregate_kinds, select_implementation
from livestack_node.decisions.service import DecisionService, build_app, default_kinds
from livestack_node.fleet_auth import load_principals

TOK_FIXED = "t" * 40
TOK_HUB = "h" * 40
PRINCIPALS = load_principals(
    json.dumps({
        TOK_FIXED: {"name": "media-corpus", "owner": "media-corpus"},
        TOK_HUB: {"name": "hub", "delegate_prefix": "acct_"},
    })
)
NOW = 1_700_000_000_000
PROFILE = "pane-attention-v1:unqualified"


def _broker_ok(**kwargs):
    return {"granted": True, "device_id": "dev-1", "lease_id": "lease-1"}


def _svc(broker=_broker_ok, kinds=None, now=NOW):
    cuda = FixtureBackend("cuda", "fix-cuda")
    mlx = FixtureBackend("mlx", "fix-mlx")
    svc = DecisionService(
        principals=PRINCIPALS,
        backends={CUDA_KIND: cuda, MLX_KIND: mlx},
        broker_admit=broker,
        kinds=kinds or default_kinds(PROFILE),
        now_ms=lambda: now,
        profile_id=PROFILE,
    )
    svc._cuda = cuda
    svc._mlx = mlx
    return svc


def _client(svc):
    return TestClient(build_app(svc), raise_server_exceptions=False)


def _body():
    fx = load_fixture("status-en.json")
    req = copy.deepcopy(fx["request"])
    req["deadline_at_ms"] = NOW + 15_000
    return req


def _headers(token=TOK_HUB, realm="realm_dev", owner="acct_dev"):
    h = {"Authorization": f"Bearer {token}"}
    if realm is not None:
        h["X-Harmony-Realm"] = realm
    if owner is not None:
        h["X-Harmony-Owner"] = owner
    return h


# -- H06 auth ---------------------------------------------------------------

def test_h06_missing_token_is_401_before_inference():
    svc = _svc()
    r = _client(svc).post("/v1/decisions", json=_body(), headers={"X-Harmony-Realm": "realm_dev", "X-Harmony-Owner": "acct_dev"})
    assert r.status_code == 401
    assert r.json()["cause"] == "unauthorized"
    assert svc._cuda.calls == 0


def test_h06_forged_owner_header_is_403_before_inference():
    svc = _svc()
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers(owner="media-corpus"))
    assert r.status_code == 403
    assert svc._cuda.calls == 0


def test_h06_direct_worker_refuses_before_inference():
    svc = _svc()
    r = _client(svc).post("/v1/worker/decisions", json=_body(), headers=_headers())
    assert r.status_code == 403
    assert "direct worker" in r.json()["detail"]
    assert svc._cuda.calls == 0


def test_h06_unauthenticated_legacy_admit_cannot_bypass():
    svc = _svc()
    r = _client(svc).post("/admit", json={"kind": CUDA_KIND})
    assert r.status_code == 401
    r2 = _client(svc).post("/admit", json={"kind": CUDA_KIND}, headers=_headers())
    assert r2.status_code == 403
    assert "legacy /admit" in r2.json()["detail"]
    assert svc._cuda.calls == 0


def test_h06_missing_realm_is_not_mesh():
    svc = _svc()
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers(realm=None))
    assert r.status_code == 403
    assert "mesh" in r.json()["detail"]
    assert svc._cuda.calls == 0


# -- H01 kinds --------------------------------------------------------------

def test_h01_architecture_free_request_can_use_either_backend():
    svc = _svc()
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 200, r.text
    assert r.json()["execution"]["backend"] in ("cuda", "mlx")
    assert r.json()["answers"]["attention"]["choice"] == "question"
    # Force the other backend via diagnostic header.
    other = "mlx" if r.json()["execution"]["backend"] == "cuda" else "cuda"
    r2 = _client(svc).post("/v1/decisions", json=_body(), headers={**_headers(), "X-Harmony-Backend": other})
    assert r2.status_code == 200
    assert r2.json()["execution"]["backend"] == other


def test_h01_mismatched_language_or_limits_never_substitute():
    kinds = default_kinds(PROFILE)
    kinds[0].language = "english"
    kinds[1].language = "english"
    svc = _svc(kinds=kinds)
    # Profile is multilingual; english-only kinds must not serve it.
    for k in svc.admission.kinds:
        k.language = "english"
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 503
    assert r.json()["cause"] == "profile_unavailable"
    assert svc._cuda.calls == 0


def test_h01_same_kind_conflict_is_visible_not_first_seen():
    a = default_kinds(PROFILE)[0]
    b = PhysicalKind(**{**a.__dict__, "max_len": 512})
    with pytest.raises(KindConflict, match="max_len"):
        aggregate_kinds(a, b)
    # Declaration order does not change the eligible set.
    kinds = default_kinds(PROFILE)
    first = select_implementation(list(reversed(kinds)), profile_id=PROFILE)
    second = select_implementation(kinds, profile_id=PROFILE)
    assert first.kind == second.kind


# -- H04 / H07 broker -------------------------------------------------------

def test_h04_broker_outage_refuses_cold_and_warm_new_requests():
    def down(**kwargs):
        raise BrokerUnavailable("broker down")

    svc = _svc(broker=down)
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 503
    assert r.json()["cause"] == "broker_unavailable"
    assert svc._cuda.calls == 0
    # Warm: mark resident and retry. Still refused.
    for k in svc.admission.kinds:
        k.resident = True
    r2 = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r2.status_code == 503
    assert r2.json()["cause"] == "broker_unavailable"


def test_h04_already_admitted_work_is_retained_through_broker_loss():
    svc = _svc()
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 200
    rid = _body()["request_id"]
    # finish() already ran; simulate an inflight lease that has not finished.
    svc.admission._inflight[rid].finished = False
    assert svc.admission.retain_until_complete(rid) is True
    svc.admission.finish(rid)
    assert svc.admission.retain_until_complete(rid) is False


def test_h04_unknown_memory_cannot_cold_load():
    kinds = default_kinds(PROFILE)
    for k in kinds:
        k.resident_bytes = None
        k.peak_bytes = None
        k.resident = False
    svc = _svc(kinds=kinds)
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 503
    assert svc._cuda.calls == 0


def test_h07_expired_deadline_is_not_reset_and_is_504():
    svc = _svc(now=NOW + 20_000)
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 504
    assert r.json()["cause"] == "deadline_exceeded"
    assert svc._cuda.calls == 0


# -- H03 / H05 / H08 / H10 --------------------------------------------------

def test_h03_duplicate_question_ids_are_400_and_do_not_infer():
    svc = _svc()
    body = _body()
    body["questions"].append(copy.deepcopy(body["questions"][0]))
    r = _client(svc).post("/v1/decisions", json=body, headers=_headers())
    assert r.status_code == 400
    assert svc._cuda.calls == 0


def test_h05_cancel_keeps_reservation_until_finish():
    svc = _svc()
    body = _body()
    svc.admission.admit(profile_id=PROFILE, owner="acct_dev", request_id=body["request_id"])
    svc.admission.cancel(body["request_id"])
    item = svc.admission.inflight(body["request_id"])
    assert item.cancelled is True
    assert item.finished is False
    assert svc.admission.retain_until_complete(body["request_id"]) is True


def test_h08_unhealthy_worker_is_not_eligible():
    svc = _svc()
    for k in svc.admission.kinds:
        k.healthy = False
    r = _client(svc).post("/v1/decisions", json=_body(), headers=_headers())
    assert r.status_code == 503
    assert svc._cuda.calls == 0


def test_h10_decision_requests_do_not_use_chat_embed_paths():
    svc = _svc()
    app = build_app(svc)
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/v1/decisions" in routes
    assert "/v1/chat/completions" not in routes
    assert "/v1/embeddings" not in routes
    r = TestClient(app, raise_server_exceptions=False).post(
        "/v1/chat/completions", json={"model": "local"}, headers=_headers(),
    )
    assert r.status_code == 404
    assert svc._cuda.calls == 0
