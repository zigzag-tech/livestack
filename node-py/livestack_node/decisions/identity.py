"""Canonical identity digests for evidence, candidates, and slates.

The host mints these. Observation timestamps that only reflect rereading
unchanged evidence stay outside the content hash. Adding provenance does not
change a candidate id. Future observations cannot be mixed into a past id.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "benchday.decision.v1"
PACKING_VERSION = "decision-pack-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return "sha256:" + sha256_hex(canonical_json(value))


EVIDENCE_FIELDS = (
    "account_domain",
    "realm",
    "node",
    "pane_incarnation",
    "conversation_id",
    "binding_generation",
    "message_ids",
    "content_hashes",
    "turn_epoch",
    "lifecycle_generation",
    "human_input_generation",
)


def evidence_revision(identity: Mapping[str, Any]) -> str:
    missing = [f for f in EVIDENCE_FIELDS if f not in identity]
    if missing:
        raise ValueError(f"evidence identity missing {missing}")
    extra = sorted(set(identity) - set(EVIDENCE_FIELDS))
    if extra:
        raise ValueError(f"evidence identity has extra fields {extra}; timestamps must not enter the digest")
    payload = {k: identity[k] for k in EVIDENCE_FIELDS}
    if not isinstance(payload["message_ids"], list) or not isinstance(payload["content_hashes"], list):
        raise ValueError("message_ids and content_hashes must be lists")
    if len(payload["message_ids"]) != len(payload["content_hashes"]):
        raise ValueError("message_ids and content_hashes length mismatch")
    return digest(payload)


def candidate_id(account_domain: str, normalized_reply: str) -> str:
    if not account_domain:
        raise ValueError("candidate id requires an account authorization domain")
    return digest({"account_domain": account_domain, "normalized_reply": normalized_reply})


def slate_id(
    *,
    ordered_candidate_ids: Sequence[str],
    evidence_revision_hex: str,
    vocabulary_revision: str,
    policy_revision: str,
    question_id: str | None = None,
) -> str:
    return digest({
        "ordered_candidate_ids": list(ordered_candidate_ids),
        "evidence_revision": evidence_revision_hex,
        "vocabulary_revision": vocabulary_revision,
        "policy_revision": policy_revision,
        "question_id": question_id,
    })


def vocabulary_revision(account_domain: str, commands: Iterable[str], extractor_policy: str) -> str:
    return digest({
        "account_domain": account_domain,
        "commands": list(commands),
        "extractor_policy": extractor_policy,
    })
