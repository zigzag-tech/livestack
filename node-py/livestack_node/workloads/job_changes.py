"""Incremental, owner-filtered job listing for machine-local readers
(pipelines-are-streams 4.1): `GET /v1/workloads/job-changes`.

Read-only over the authority's own tables. Every field is the authority's own
word: `state` is the job state, `outcome.kind` comes from the completion
(`product_failure` -> `product`, `infrastructure` -> `infrastructure`) or, when
a job ended without a completion, from its typed cause (`unplaceable` /
`capability_absent` -> `refused`, anything else -> `unknown`). A succeeded job
carries no outcome. `placed` is never emitted: this authority claims and starts
an attempt in one step, so a claimed job is already `running`.

Paging: rows have `updated_ms > updated_since`, oldest-updated first. When the
page is cut, trailing rows that share the last kept row's millisecond are
deferred to the next page (unless that would leave the page empty), so resuming
at the last returned `updated_ms` never skips a row. Bounds: at most
MAX_LIMIT rows and MAX_BYTES bytes per response; the rest is counted in
`truncated`.
"""
from __future__ import annotations

import json

from .model import WorkloadError

MAX_LIMIT = 256
# Below the 32 KiB frame a daemon loopback reader accepts.
MAX_BYTES = 30 * 1024

_OUTCOME_OF_COMPLETION = {"product_failure": "product", "infrastructure": "infrastructure"}
_REFUSED_CAUSES = ("unplaceable", "capability_absent")
_TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def _ms(seconds):
    return int(seconds * 1000)


def _clip(value, limit):
    text = "" if value is None else str(value)
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def _num(value):
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _requires(spec):
    parts = [f"{key}={_num(value)}" for key, value in sorted((spec.get("need") or {}).items())]
    parts += [f"selector.{key}={value}" for key, value in sorted((spec.get("selector") or {}).items())]
    return _clip(",".join(parts), 160) or "none"


def _outcome(state, result, cause):
    if state == "succeeded":
        return None
    kind = _OUTCOME_OF_COMPLETION.get((result or {}).get("outcome")) if isinstance(result, dict) else None
    cause_kind = cause.get("kind") if isinstance(cause, dict) else None
    if kind is None:
        kind = "refused" if cause_kind in _REFUSED_CAUSES else "unknown"
    reason = ""
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        reason = result["result"].get("error") or ""
    return {"kind": kind, "reason": _clip(reason or cause_kind or state, 240) or state}


def _row(db_row, attempt):
    state = db_row["state"]
    spec = json.loads(db_row["spec"])
    labels = json.loads(db_row["labels"] or "{}")
    result = json.loads(db_row["result"]) if db_row["result"] else None
    cause = json.loads(db_row["cause"]) if "cause" in db_row.keys() and db_row["cause"] else None
    row = {
        "id": _clip(db_row["id"], 160),
        "owner": _clip(labels.get("owner") or db_row["owner"], 128),
        "handler": _clip(spec.get("handler"), 128),
        "requires": _requires(spec),
        "state": state,
        "queued_ms": _ms(db_row["created"]),
        "updated_ms": _ms(db_row["updated"]),
    }
    if attempt is not None:
        row["attempt"] = {"id": _clip(attempt["id"], 128), "worker": _clip(attempt["worker"], 128),
                          "lease_until_ms": _ms(attempt["expires"]), "started_ms": _ms(attempt["created"])}
    if state in _TERMINAL:
        row["finished_ms"] = row["updated_ms"]
        outcome = _outcome(state, result, cause)
        if outcome:
            row["outcome"] = outcome
    return row


def job_changes(store, *, updated_since=0, owner=None, limit=MAX_LIMIT):
    """The page described in the module docstring: {now_ms, jobs, truncated}."""
    if isinstance(updated_since, bool) or not isinstance(updated_since, int) or updated_since < 0:
        raise WorkloadError("updated_since must be a non-negative integer (ms)")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise WorkloadError("limit must be a positive integer")
    limit = min(limit, MAX_LIMIT)
    if owner is not None and (not isinstance(owner, str) or not 0 < len(owner) <= 128):
        raise WorkloadError("invalid owner")
    where = "CAST(updated*1000 AS INTEGER) > ?"
    args = [updated_since]
    if owner is not None:
        where += " AND COALESCE(json_extract(labels,'$.owner'), owner) = ?"
        args.append(owner)
    with store.transaction() as db:
        store._expire(db, store.clock())
        total = db.execute(f"SELECT count(*) FROM jobs WHERE {where}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY updated, id LIMIT ?",
                          args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        jobs = []
        for db_row in rows:
            attempt = db.execute("SELECT id,worker,expires,created FROM attempts WHERE job=? "
                                 "ORDER BY fence DESC LIMIT 1", (db_row["id"],)).fetchone()
            jobs.append(_row(db_row, attempt))
        now_ms = _ms(store.clock())
    # Bytes: drop rows from the end until the page fits.
    while jobs and len(json.dumps({"now_ms": now_ms, "jobs": jobs, "truncated": total},
                                  separators=(",", ":")).encode()) > MAX_BYTES:
        jobs.pop()
        more = True
    # Never split one millisecond across pages (keep at least one row).
    if more and jobs:
        last = jobs[-1]["updated_ms"]
        kept = [job for job in jobs if job["updated_ms"] != last]
        if kept:
            jobs = kept
    return {"now_ms": now_ms, "jobs": jobs, "truncated": total - len(jobs)}
