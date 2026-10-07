"""Bounded Fact v1 identity snapshot helpers.

These records are correlation metadata. Creating a snapshot does not make a
placement decision and does not write to the decision ledger.
"""
from __future__ import annotations

import threading
import time
import uuid

FACT_VERSION = 1
MAX_FACT_TTL_MS = 600_000
DEFAULT_FACT_TTL_MS = 60_000
MAX_IDENTITY_BYTES = 256


def valid_identity(value, *, max_bytes=MAX_IDENTITY_BYTES):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (len(encoded) <= max_bytes and
            not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value))


def hosted_on_fact(*, authority_kind, authority_id, resource_namespace,
                   resource_id, host_id, generation, sequence, observed_at_ms,
                   ttl_ms=DEFAULT_FACT_TTL_MS):
    """Build one explicit source-owned Fact v1 edge or fail with a named error."""
    values = (authority_kind, authority_id, resource_namespace, resource_id, host_id, generation)
    if not all(valid_identity(value, max_bytes=220) for value in values):
        raise ValueError("invalid_identity")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("invalid_fence")
    if not isinstance(observed_at_ms, int) or isinstance(observed_at_ms, bool) or observed_at_ms <= 0:
        raise ValueError("invalid_timestamp")
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or not 0 < ttl_ms <= MAX_FACT_TTL_MS:
        raise ValueError("invalid_ttl")
    source = f"{resource_namespace}:{resource_id}"
    target = f"benchday:host:{host_id}"
    if not valid_identity(source) or not valid_identity(target):
        raise ValueError("invalid_identity")
    return {
        "v": FACT_VERSION,
        "subject": {"kind": "edge", "from": source, "edge_type": "hosted_on", "to": target},
        "attribute": "present",
        "kind": "observed",
        "authority": {"kind": authority_kind, "id": authority_id},
        "value": True,
        "observed_at_ms": observed_at_ms,
        "fence": {"generation": generation, "sequence": sequence},
        "ttl_ms": ttl_ms,
        "scope": {"kind": "owner_account"},
    }


class IdentitySnapshotPublisher:
    """Owns one process generation and monotonically increasing complete cuts."""

    def __init__(self, authority_kind, authority_id, *, ttl_ms=DEFAULT_FACT_TTL_MS):
        if not valid_identity(authority_kind, max_bytes=64):
            raise ValueError("invalid_authority_kind")
        if not valid_identity(authority_id, max_bytes=220):
            raise ValueError("invalid_authority_id")
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or not 0 < ttl_ms <= MAX_FACT_TTL_MS:
            raise ValueError("invalid_ttl")
        self.authority_kind = authority_kind
        self.authority_id = authority_id
        self.ttl_ms = ttl_ms
        self.generation = uuid.uuid4().hex
        self.sequence = 0
        self._lock = threading.Lock()

    def snapshot(self, relations, *, now_ms=None):
        """Return a full current cut; the caller replaces its previous cut."""
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            self.sequence += 1
            sequence = self.sequence
        facts = [
            hosted_on_fact(
                authority_kind=self.authority_kind,
                authority_id=self.authority_id,
                resource_namespace=relation["resource_namespace"],
                resource_id=relation["resource_id"],
                host_id=relation["host_id"],
                generation=self.generation,
                sequence=sequence,
                observed_at_ms=relation.get("observed_at_ms", now_ms),
                ttl_ms=min(relation.get("ttl_ms", self.ttl_ms), self.ttl_ms),
            )
            for relation in relations
        ]
        return {
            "v": FACT_VERSION,
            "generation": self.generation,
            "sequence": sequence,
            "observed_at_ms": now_ms,
            "facts": facts,
        }

