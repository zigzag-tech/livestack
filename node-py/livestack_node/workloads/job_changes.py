"""Incremental owner-filtered job listing for authority readers.

Read-only over the authority's own tables. Every field is the authority's own
word: `state` is the job state, `outcome.kind` comes from the completion
(`product_failure` -> `product`, `infrastructure` -> `infrastructure`) or, when
a job ended without a completion, from its typed cause (`unplaceable` /
`capability_absent` -> `refused`, anything else -> `unknown`). A succeeded job
carries no outcome. `placed` is never emitted: this authority claims and starts
an attempt in one step, so a claimed job is already `running`.

The admin endpoint uses strict `updated_ms > updated_since` polling and defers
timestamp ties to the next page. The fixed-owner reader endpoint replays its
timestamp boundary and uses `(updated_ms, job_id)` cursors within a poll.
Responses are bounded by MAX_LIMIT rows and MAX_BYTES bytes. The admin endpoint
reports the exact withheld count; the reader endpoint reports whether another
page remains without scanning and counting the full owner history.
"""
from __future__ import annotations

import json

from .model import WorkloadError, encode, name

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
        inner = result["result"]
        reason = inner.get("error") or (cause or {}).get("evidence", {}).get("excerpt") or ""
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


def job_changes(store, *, updated_since=0, owner=None, limit=MAX_LIMIT,
                after_job_id=None, inclusive=False, with_cursor=False, bounded=False):
    """Read one bounded projection. `bounded` avoids a total-row count for source polling."""
    if isinstance(updated_since, bool) or not isinstance(updated_since, int) or updated_since < 0:
        raise WorkloadError("updated_since must be a non-negative integer (ms)")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise WorkloadError("limit must be a positive integer")
    limit = min(limit, MAX_LIMIT)
    if owner is not None and (not isinstance(owner, str) or not 0 < len(owner) <= 128):
        raise WorkloadError("invalid owner")
    if after_job_id is not None:
        try:
            name(after_job_id, "after_job_id")
        except WorkloadError as error:
            raise WorkloadError("invalid after_job_id") from error
    updated_ms = "CAST(updated*1000 AS INTEGER)"
    if after_job_id is not None:
        where = f"({updated_ms} > ? OR ({updated_ms} = ? AND id > ?))"
        args = [updated_since, updated_since, after_job_id]
    else:
        where = f"{updated_ms} {'>=' if inclusive else '>'} ?"
        args = [updated_since]
    if owner is not None:
        where += " AND COALESCE(NULLIF(json_extract(labels,'$.owner'), ''), owner) = ?"
        args.append(owner)
    with store.transaction() as db:
        store._expire(db, store.clock())
        total = None if bounded else db.execute(f"SELECT count(*) FROM jobs WHERE {where}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {updated_ms}, id LIMIT ?",
                          args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        ids = [row["id"] for row in rows]
        attempts = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            latest = db.execute(
                "SELECT job,id,worker,expires,created FROM ("
                "SELECT job,id,worker,expires,created,"
                "ROW_NUMBER() OVER (PARTITION BY job ORDER BY fence DESC) AS rank "
                f"FROM attempts WHERE job IN ({placeholders})"
                ") WHERE rank=1", ids).fetchall()
            attempts = {attempt["job"]: attempt for attempt in latest}
        jobs = [_row(db_row, attempts.get(db_row["id"])) for db_row in rows]
        now_ms = _ms(store.clock())

    def response():
        value = {"now_ms": now_ms, "jobs": jobs,
                 "truncated": (1 if more else 0) if bounded else total - len(jobs)}
        if with_cursor:
            value["owner"] = owner
            value["next_cursor"] = ({"updated_since": jobs[-1]["updated_ms"],
                                     "after_job_id": jobs[-1]["id"]} if jobs else None)
        return value

    # Bytes: drop rows from the end until the complete wire response fits.
    while jobs and len(encode(response()).encode()) > MAX_BYTES:
        jobs.pop()
        more = True
    # Never split one millisecond across pages (keep at least one row).
    if more and jobs and not with_cursor:
        last = jobs[-1]["updated_ms"]
        kept = [job for job in jobs if job["updated_ms"] != last]
        if kept:
            jobs = kept
    return response()
