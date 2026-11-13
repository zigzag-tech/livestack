"""Authority-owned source joins; no payload, prompt, worker choice or credentials."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .model import WorkloadError, encode

MAX_BYTES = 512
MAX_KINDS = 8
RESERVED_LABELS = frozenset(("source_kind", "source_turn_id", "source_principal", "current_explanation"))
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z", re.ASCII)
_TURN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)


def turn_id(value):
    if not isinstance(value, str) or not _TURN.fullmatch(value):
        raise WorkloadError("provenance_turn_id_invalid")
    return value


def kinds(value, role):
    if (not isinstance(value, (list, tuple)) or len(value) > MAX_KINDS or
            any(not isinstance(v, str) or not _TOKEN.fullmatch(v) for v in value) or
            len(set(value)) != len(value) or value and role not in ("caller", "admin")):
        raise ValueError("provenance_kinds must be distinct bounded caller/admin grants")
    return tuple(value)


@dataclass(frozen=True)
class SourceProvenance:
    kind: str
    turn_id: str
    principal: str
    owner: str

    def record(self):
        value = dict(v=1, kind=self.kind, turn_id=turn_id(self.turn_id),
                     principal=self.principal, owner=self.owner)
        if any(not isinstance(v, str) or not _TOKEN.fullmatch(v)
               for v in (self.kind, self.principal, self.owner)):
            raise WorkloadError("provenance_identity_invalid")
        encode(value, MAX_BYTES)
        return value


def authorize(principal, body):
    tags = body.get("labels", {})
    if isinstance(tags, dict) and RESERVED_LABELS.intersection(tags):
        raise WorkloadError("provenance_labels_forbidden")
    if "provenance" not in body:
        return None
    raw = body["provenance"]
    if (not isinstance(raw, dict) or set(raw) != {"v", "kind", "turn_id"} or
            type(raw.get("v")) is not int or raw["v"] != 1):
        raise WorkloadError("provenance_envelope_invalid")
    encode(raw, MAX_BYTES)
    kind = raw.get("kind")
    if not isinstance(kind, str) or not _TOKEN.fullmatch(kind):
        raise WorkloadError("provenance_kind_invalid")
    source_turn = turn_id(raw.get("turn_id"))
    if principal.role not in ("caller", "admin") or kind not in principal.provenance_kinds:
        raise WorkloadError("provenance_not_authorized", 403)
    # _authorized_labels already validated this delegated owner against the
    # credential's prefix. Neither stamp is accepted from the request envelope.
    source = SourceProvenance(kind, source_turn, principal.id, tags.get("owner") or principal.id)
    source.record()
    return source


def migrate(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    for field, declaration in (("source_provenance", "TEXT"), ("source_observed", "REAL")):
        if field not in columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {field} {declaration}")
    db.execute("CREATE INDEX IF NOT EXISTS jobs_source_turn_changes ON jobs("
               "COALESCE(NULLIF(json_extract(labels,'$.owner'),''),owner),"
               "json_extract(source_provenance,'$.turn_id'),CAST(updated*1000 AS INTEGER),id)")
    db.execute("CREATE INDEX IF NOT EXISTS jobs_source_expiry ON jobs(source_observed)")


def expire(db, now, seconds):
    # One indexed statement; the containing jobs already have active/terminal
    # count caps. Retain exempts job data, not this separately declared metadata.
    db.execute("UPDATE jobs SET source_provenance=NULL,source_observed=NULL "
               "WHERE source_observed<=?", (now - seconds,))
