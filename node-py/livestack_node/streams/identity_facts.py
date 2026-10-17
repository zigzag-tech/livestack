"""Bounded complete-cut paging for source-owned identity Fact streams."""
from __future__ import annotations

import json
from typing import Any, Dict, List

FLEET_IDENTITY_FACTS = "livestack.identity-facts.fleet/1"
WORKLOAD_IDENTITY_FACTS = "livestack.identity-facts.workload/1"

MAX_IDENTITY_FACTS = 4096
MAX_IDENTITY_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_IDENTITY_PARTIAL_BYTES = 24 * 1024
MAX_IDENTITY_PAGES = 128
MAX_IDENTITY_FACT_TTL_MS = 60_000
IDENTITY_CUT_REFRESH_S = 15.0

# The daemon adds these fields around a state partial. Reserve for a longer
# source id and integer fences than the Livestack sources currently use, so
# the actual complete partial remains within the contract byte bound.
_MAX_SOURCE_BYTES = 512
_MAX_FENCE = (1 << 63) - 1


class IdentityCutError(ValueError):
    """A named validation or packing failure; callers must send no pages."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _valid_text(value: Any, max_bytes: int = 256) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= max_bytes and not any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def _validate_cut(cut: Any, *, authority_id: str, resource_namespace: str) -> dict:
    if not isinstance(cut, dict) or not _integer(cut.get("v"), minimum=1) or cut.get("v") != 1:
        raise IdentityCutError("identity_cut_schema_invalid")
    if set(cut) - {"v", "generation", "sequence", "observed_at_ms", "facts", "source_state"}:
        raise IdentityCutError("identity_cut_schema_invalid")
    generation = cut.get("generation")
    sequence = cut.get("sequence")
    observed_at_ms = cut.get("observed_at_ms")
    facts = cut.get("facts")
    if not _valid_text(generation, 220) or not _integer(sequence, minimum=1) or not _integer(
            observed_at_ms, minimum=1):
        raise IdentityCutError("identity_cut_fence_invalid")
    if not isinstance(facts, list):
        raise IdentityCutError("identity_cut_facts_invalid")
    if len(facts) > MAX_IDENTITY_FACTS:
        raise IdentityCutError("identity_cut_fact_count_exceeded")

    prefix = resource_namespace + ":"
    seen = set()
    for fact in facts:
        if not isinstance(fact, dict) or not _integer(fact.get("v"), minimum=1) or fact.get("v") != 1:
            raise IdentityCutError("identity_fact_schema_invalid")
        if set(fact) != {"v", "subject", "attribute", "kind", "authority", "value",
                         "observed_at_ms", "fence", "ttl_ms", "scope"}:
            raise IdentityCutError("identity_fact_schema_invalid")
        subject = fact.get("subject")
        authority = fact.get("authority")
        fence = fact.get("fence")
        scope = fact.get("scope")
        if (not isinstance(subject, dict) or
                set(subject) != {"kind", "from", "edge_type", "to"} or subject.get("kind") != "edge" or
                subject.get("edge_type") != "hosted_on" or
                not isinstance(authority, dict) or
                authority != {"kind": "livestack", "id": authority_id} or
                fact.get("attribute") != "present" or fact.get("kind") != "observed" or
                fact.get("value") is not True or scope != {"kind": "owner_account"}):
            raise IdentityCutError("identity_fact_authority_or_scope_invalid")
        source = subject.get("from")
        target = subject.get("to")
        if (not _valid_text(source) or not source.startswith(prefix) or
                not _valid_text(source[len(prefix):]) or
                not _valid_text(target) or not target.startswith("benchday:host:") or
                not _valid_text(target[len("benchday:host:"):])):
            raise IdentityCutError("identity_fact_subject_invalid")
        if source in seen:
            raise IdentityCutError("identity_fact_duplicate_subject")
        seen.add(source)
        if (not isinstance(fence, dict) or
                set(fence) != {"generation", "sequence"} or
                fence.get("generation") != generation or
                not _integer(fence.get("sequence"), minimum=1) or fence.get("sequence") != sequence or
                not _integer(fact.get("observed_at_ms"), minimum=1) or
                fact["observed_at_ms"] > observed_at_ms or
                not _integer(fact.get("ttl_ms"), minimum=1) or
                fact["ttl_ms"] > MAX_IDENTITY_FACT_TTL_MS):
            raise IdentityCutError("identity_fact_fence_or_ttl_invalid")

    # source_state is producer diagnostics, not part of the canonical Fact
    # snapshot carried by streams.
    canonical_cut = {"v": 1, "generation": generation, "sequence": sequence,
                     "observed_at_ms": observed_at_ms, "facts": facts}
    if len(canonical(canonical_cut)) > MAX_IDENTITY_SNAPSHOT_BYTES:
        raise IdentityCutError("identity_cut_snapshot_bytes_exceeded")
    return canonical_cut


def _page_body(cut: dict, page_index: int, page_count: int, facts: list) -> dict:
    return {"v": 1, "generation": cut["generation"], "sequence": cut["sequence"],
            "observed_at_ms": cut["observed_at_ms"], "page_index": page_index,
            "page_count": page_count, "facts": facts}


def _complete_partial_size(contract: str, page_index: int, body: dict) -> int:
    # This mirrors the daemon's complete state partial envelope, using the
    # maximum source id and 63-bit outer fences. All keys are protocol fields.
    partial = {"contract": contract, "source": "s" * _MAX_SOURCE_BYTES,
               "subject": {"page": str(page_index)}, "epoch": _MAX_FENCE,
               "seq": _MAX_FENCE, "produced_ms": _MAX_FENCE,
               "ttl_ms": MAX_IDENTITY_FACT_TTL_MS, "status": "ok", "body": body}
    return len(canonical(partial))


def pack_identity_cut(cut: Any, *, contract: str, authority_id: str,
                      resource_namespace: str) -> List[dict]:
    """Validate and page a whole Fact cut before returning any publishable page.

    Returned bodies are ordered by their stable `page_index`; each is safe to
    publish with subject `{page: str(page_index)}`. The caller must pack the entire
    cut before sending page zero.
    """
    if contract not in (FLEET_IDENTITY_FACTS, WORKLOAD_IDENTITY_FACTS):
        raise IdentityCutError("identity_contract_invalid")
    expected_namespace = {
        FLEET_IDENTITY_FACTS: "harmony:node",
        WORKLOAD_IDENTITY_FACTS: "livestack:worker",
    }[contract]
    if resource_namespace != expected_namespace or not _valid_text(authority_id, 220):
        raise IdentityCutError("identity_source_configuration_invalid")
    canonical_cut = _validate_cut(cut, authority_id=authority_id,
                                  resource_namespace=resource_namespace)
    facts = canonical_cut["facts"]

    if not facts:
        body = _page_body(canonical_cut, 0, 1, [])
        if _complete_partial_size(contract, 0, body) > MAX_IDENTITY_PARTIAL_BYTES:
            raise IdentityCutError("identity_partial_bytes_exceeded")
        return [body]

    groups: List[List[dict]] = []
    current: List[dict] = []
    for fact in facts:
        candidate = current + [fact]
        # Reserve the largest valid metadata values while packing; replacing
        # them with the final page index/count can only make the body smaller.
        probe = _page_body(canonical_cut, MAX_IDENTITY_PAGES - 1,
                           MAX_IDENTITY_PAGES, candidate)
        if _complete_partial_size(contract, MAX_IDENTITY_PAGES - 1, probe) <= MAX_IDENTITY_PARTIAL_BYTES:
            current = candidate
            continue
        if not current:
            raise IdentityCutError("identity_partial_bytes_exceeded")
        groups.append(current)
        if len(groups) >= MAX_IDENTITY_PAGES:
            raise IdentityCutError("identity_cut_page_count_exceeded")
        current = [fact]
        single = _page_body(canonical_cut, MAX_IDENTITY_PAGES - 1,
                            MAX_IDENTITY_PAGES, current)
        if _complete_partial_size(contract, MAX_IDENTITY_PAGES - 1, single) > MAX_IDENTITY_PARTIAL_BYTES:
            raise IdentityCutError("identity_partial_bytes_exceeded")
    if current:
        groups.append(current)
    if len(groups) > MAX_IDENTITY_PAGES:
        raise IdentityCutError("identity_cut_page_count_exceeded")

    pages = [_page_body(canonical_cut, index, len(groups), group)
             for index, group in enumerate(groups)]
    if any(_complete_partial_size(contract, index, page) > MAX_IDENTITY_PARTIAL_BYTES
           for index, page in enumerate(pages)):
        raise IdentityCutError("identity_partial_bytes_exceeded")
    return pages
