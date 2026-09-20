"""Drive the shipped typed-decision contract validators. No reimplementation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from livestack_node.decisions.contract import (
    CONTRACT_REVISION,
    ContractError,
    load_fixture,
    load_profile,
    validate_request,
    validate_result,
    validate_slate,
)
from livestack_node.decisions.identity import candidate_id, evidence_revision, slate_id
from livestack_node.decisions.packing import pack_request
from livestack_node.decisions.paths import DATA_DIR, REVISION_FILE

NOW = 1_700_000_000_000
FUTURE = NOW + 15_000


def _req(name: str):
    fx = load_fixture(name)
    req = copy.deepcopy(fx["request"])
    req["deadline_at_ms"] = FUTURE
    return fx, req


def test_revision_links_benchday_change():
    text = REVISION_FILE.read_text(encoding="utf-8")
    assert CONTRACT_REVISION == "typed-decision-contract-v1.0.0"
    assert "openspec/changes/route-pane-decisions-through-harmony" in text
    assert "benchday.decision.v1" in text


def test_en_zh_mixed_fixtures_and_identity_fence():
    for name in ("status-en.json", "status-zh.json", "status-mixed.json"):
        fx, req = _req(name)
        assert evidence_revision(fx["identity"]) == req["evidence_revision"]
        validate_request(req, now_ms=NOW)
    ident = load_fixture("identity.json")
    assert evidence_revision(ident["base"]) == ident["base_revision"]
    assert evidence_revision(ident["future_human_input"]) == ident["future_revision"]
    assert ident["base_revision"] != ident["future_revision"]
    with pytest.raises(ValueError, match="extra fields"):
        evidence_revision({**ident["base"], "observed_at_ms": 1})


def test_h03_missing_duplicate_extra_ids_reject_empty_slate_accepts():
    _, req = _req("status-en.json")
    missing = copy.deepcopy(req)
    missing["questions"] = []
    with pytest.raises(ContractError):
        validate_request(missing, now_ms=NOW)

    dup = copy.deepcopy(req)
    dup["questions"].append(copy.deepcopy(dup["questions"][0]))
    with pytest.raises(ContractError, match="duplicate"):
        validate_request(dup, now_ms=NOW)

    extra = copy.deepcopy(req)
    extra["account_id"] = "forged"
    with pytest.raises(ContractError):
        validate_request(extra, now_ms=NOW)

    empty = load_fixture("slate-empty.json")["slate"]
    validate_slate(empty)
    assert empty["outcome"] == "abstained"
    assert empty["chips"] == []
    selected_empty = {**empty, "outcome": "selected"}
    with pytest.raises(ContractError):
        validate_slate(selected_empty)


def test_h02_packing_matches_frozen_fixture():
    fx, req = _req("status-en.json")
    profile = load_profile("pane-attention-v1.json")
    packed = pack_request(req["state"], req["questions"], profile)
    frozen = load_fixture("packing-status-en.json")
    assert packed["ok"] is True
    assert packed["packed_state_hash"] == frozen["packed_state_hash"]
    assert packed["rows"][0]["token_ids"] == frozen["rows"][0]["token_ids"]
    assert packed["rows"][0]["markers"] == frozen["rows"][0]["markers"]
    assert len(packed["rows"][0]["markers"]) == 6


def test_h03_answer_id_set_and_nan_reject():
    _, req = _req("status-en.json")
    base = {
        "schema_version": "benchday.decision.v1",
        "request_id": req["request_id"],
        "evidence_revision": req["evidence_revision"],
        "profile_id": req["profile_id"],
        "outcome": "ok",
        "answers": {
            "attention": {
                "type": "choice",
                "choice": "question",
                "probabilities": {
                    "question": 0.9, "working": 0.02, "self_waiting": 0.02,
                    "finished_turn": 0.02, "idle": 0.02, "unknown": 0.02,
                },
            }
        },
        "execution": {
            "backend": "cuda", "implementation_id": "fix", "model_revision": "fix",
            "tokenizer_hash": "sha256:aa", "calibration_hash": "unqualified",
            "packing_version": "decision-pack-v1", "precision": "fp32",
            "queue_ms": 0, "load_ms": 0, "inference_ms": 1, "total_ms": 1,
        },
        "coverage": {"original_tokens": 10, "used_tokens": 10, "omitted_turns": 0, "decisive_span_complete": True},
    }
    validate_result(base, request=req)
    extra = copy.deepcopy(base)
    extra["answers"]["bonus"] = {"type": "noul", "probability": 0.1}
    with pytest.raises(ContractError, match="answer ids"):
        validate_result(extra, request=req)
    nan = copy.deepcopy(base)
    nan["answers"]["attention"]["probabilities"]["question"] = float("nan")
    with pytest.raises(ContractError):
        validate_result(nan, request=req)


def test_c05_slate_id_ignores_generated_count():
    empty = load_fixture("slate-empty.json")["slate"]
    assert slate_id(
        ordered_candidate_ids=[],
        evidence_revision_hex=empty["evidence_revision"],
        vocabulary_revision=empty["vocabulary_revision"],
        policy_revision=empty["policy_revision"],
    ) == empty["slate_id"]
    other = slate_id(
        ordered_candidate_ids=[candidate_id("acct_dev", "yes")],
        evidence_revision_hex=empty["evidence_revision"],
        vocabulary_revision=empty["vocabulary_revision"],
        policy_revision=empty["policy_revision"],
    )
    assert other != empty["slate_id"]


def test_deadline_zero_and_expired_reject():
    _, req = _req("status-en.json")
    req0 = {**req, "deadline_at_ms": 0}
    with pytest.raises(ContractError):
        validate_request(req0, now_ms=NOW)
    req_exp = {**req, "deadline_at_ms": NOW - 1}
    with pytest.raises(ContractError, match="expired"):
        validate_request(req_exp, now_ms=NOW)


def test_cuda_extra_excludes_mlx_and_mlx_extra_excludes_torch():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pin = json.loads((DATA_DIR / "extras-pin.json").read_text(encoding="utf-8"))
    assert f"{pin['cuda_extra_name']} =" in text or f'{pin["cuda_extra_name"]} =' in text
    # Slice extras blocks.
    cuda = _extra_block(text, "decision-cuda")
    mlx = _extra_block(text, "decision-mlx")
    for pkg in pin["cuda_must_include"]:
        assert pkg in cuda
    for pkg in pin["cuda_must_exclude"]:
        assert pkg not in cuda
    for pkg in pin["mlx_must_include"]:
        assert pkg in mlx
    for pkg in pin["mlx_must_exclude"]:
        assert pkg not in mlx


def _extra_block(text: str, name: str) -> str:
    start = text.index(f"{name} = [")
    end = text.index("]", start)
    return text[start:end].lower()
