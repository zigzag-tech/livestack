"""`harmony.workload/1`: the fleet broker as the one-per-realm authority for workload intents.

The ladder is `submitted < admitted < placed < running < done`; the terminals other than `done` are
`refused`, `failed`, `cancelled` and `expired`. EVERY non-`done` terminal partial carries an
`attribution` (`workload` | `infra` | `unknown`) and a `reason`. The service mints every status partial
after submit; the requester may only submit and (before `placed`) cancel.

What it does NOT do (design D3, non-goals): it decides nothing about admission or placement. The
existing workload authority (`livestack_node.workloads`, a `WorkloadStore`) admits, places and runs;
this module submits to it through an injected backend, watches the job, and publishes the rung the job
reached. Large inputs travel by reference: `spec` must be a `stream_ref`; an inline payload is refused
(`forbidden_body_field`), an oversized body is refused (`oversize`).

Mapping of the store's job onto the ladder (`rung_of`) and of its terminal causes onto attribution
(`classify_job`):

  queued (no attempt)                        -> admitted
  running, no progress reported yet          -> placed
  running, progress reported                 -> running
  succeeded                                  -> done
  failed / infrastructure, lease expired,    -> failed,   attribution infra   (worker stopped renewing)
    worker lost, transport lost, other
    infrastructure outcome
  failed / product_failure                   -> failed,   attribution workload (the handler reported it)
  failed / infrastructure + a typed `cause`  -> failed,   attribution workload (the job's own declared
    (resource limit the job itself declared)                                    limit; a retry hits it again)
  cancelled                                  -> cancelled, attribution unknown (the requester's decision)
  expired (execution deadline)               -> expired,   attribution unknown
  job missing from the authority             -> failed,   attribution infra   (`job_missing_from_authority`)
  anything else                              -> attribution unknown, with the reason it was given

Durability: `IntentLedger` is a small SQLite file (stdlib `sqlite3`) holding one row per intent: its
body, the current partial and the monotonic per-intent `seq`. The existing job store keys jobs by
(owner, request_key); the intent ledger is what maps `intent_id -> job` and carries `seq`, so a restart
re-announces every non-terminal intent under the new authority epoch and an idempotent re-submit returns
the current partial. Storage bounds (rule 10), all enforced by `IntentLedger` itself on write and named
in the catalog's `durable` block for this contract: at most `MAX_LIVE`=1024 non-terminal rows, at most
`MAX_BYTES`=4 MiB of row text in total, terminal rows deleted after `RETAIN_TERMINAL_S`=7 days, and a
non-terminal row older than `EXPIRE_AFTER_S`=30 days is ended `expired` (never silently dropped).

Standard library only; relative imports only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .common import (MAX_BODY_BYTES, REFUSAL_FORBIDDEN_FIELD, REFUSAL_OVERSIZE, REFUSAL_SCHEMA_INVALID,
                     REFUSAL_STEP_NOT_ALLOWED, REFUSAL_UNAVAILABLE, Clock, bounded, body_size, optional)
from ..workloads.result_manifest import workload_result_manifest

REANNOUNCE_PACE_S = 0.025  # <= 40 frames/s: the ingress budget is 50/s per connection
CONTRACT = "harmony.workload/1"
LADDER = ("submitted", "admitted", "placed", "running", "done")
TERMINAL_BAD = ("refused", "failed", "cancelled", "expired")
TERMINAL = ("done",) + TERMINAL_BAD
ATTRIBUTIONS = ("workload", "infra", "unknown")

MAX_LIVE = 1024
MAX_BYTES = 4 * 1024 * 1024
RETAIN_TERMINAL_S = 604800
EXPIRE_AFTER_S = 2592000

_INTENT_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SUBMIT_FIELDS = {"intent_id", "kind", "spec", "selector", "owner"}


class BackendRefused(Exception):
    """The job authority refused the submission; `status` is its HTTP-style class (400, 409, 429, 503...)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


# --- classification ----------------------------------------------------------------------------

def classify_job(job: dict) -> dict:
    """Attribution and reason for a terminal job (see the module table). Never raises, never empty."""
    state = job.get("state")
    reason = str(job.get("reason") or "")
    completion = job.get("result") if isinstance(job.get("result"), dict) else {}
    outcome = completion.get("outcome")
    result = completion.get("result") if isinstance(completion.get("result"), dict) else {}
    error, detail = str(result.get("error") or ""), str(result.get("detail") or "")
    if state == "cancelled":
        return {"attribution": "unknown", "reason": bounded(reason or "cancelled", 240)}
    if state == "expired":
        return {"attribution": "unknown", "reason": bounded(reason or "execution deadline expired", 240)}
    if outcome == "product_failure":
        text = f"handler_reported_failure:{error}" if error else "handler_reported_failure"
        return {"attribution": "workload", "reason": bounded(f"{text} {detail}".strip(), 240)}
    if outcome == "infrastructure":
        cause = completion.get("cause") or job.get("cause")
        if isinstance(cause, dict) and cause.get("cause") == "resource_limit":
            # A limit the job itself declared: retrying runs into it again.
            return {"attribution": "workload", "reason": bounded(reason or cause.get("detail") or
                                                                      "declared_limit_exceeded", 240)}
        if error == "abandoned" or "lease" in reason:
            why = detail or reason or "attempt abandoned"
            return {"attribution": "infra", "reason": bounded(f"worker_lost:{why}", 240)}
        return {"attribution": "infra", "reason": bounded(f"infrastructure:{error or 'unspecified'} {detail}".strip(), 240)}
    return {"attribution": "unknown", "reason": bounded(reason or "no_result_recorded", 240)}


def rung_of(job: Optional[dict]) -> Tuple[str, Dict[str, Any]]:
    """`(status, extra body fields)` the job has reached; `extra` may carry attribution/reason."""
    if job is None:
        return "failed", {"attribution": "infra", "reason": "job_missing_from_authority"}
    state = job.get("state")
    attempts = job.get("attempts") or []
    live = next((a for a in reversed(attempts) if a.get("state") == "running"), None)
    count = job.get("attempt_count")
    attempt_count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(attempts)
    extra: Dict[str, Any] = {"job": bounded(job.get("id", ""), 128), "attempt": attempt_count}
    if live:
        extra["worker"] = bounded(live.get("worker", ""), 128)
        extra["host"] = bounded(live.get("host", ""), 128)
    if state == "queued":
        return "admitted", extra
    if state == "running":
        return ("running" if job.get("progress") is not None else "placed"), extra
    if state == "succeeded":
        output = job.get("output")
        if isinstance(output, dict):
            extra["output"] = output
        return "done", extra
    if state in ("failed", "cancelled", "expired"):
        return state, {**extra, **classify_job(job)}
    return "failed", {**extra, "attribution": "unknown", "reason": bounded(f"unrecognised_job_state:{state}", 200)}


# --- durable ledger ----------------------------------------------------------------------------

class LedgerFull(Exception):
    pass


class IntentLedger:
    """One row per intent. Bounded on write; see the module docstring for the bounds."""

    def __init__(self, path: str, clock: Optional[Clock] = None, max_live: int = MAX_LIVE,
                 max_bytes: int = MAX_BYTES, retain_terminal_s: int = RETAIN_TERMINAL_S,
                 expire_after_s: int = EXPIRE_AFTER_S) -> None:
        self.path, self.clock = path, optional(clock)
        self.max_live, self.max_bytes = max_live, max_bytes
        self.retain_terminal_s, self.expire_after_s = retain_terminal_s, expire_after_s
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.execute("CREATE TABLE IF NOT EXISTS intents (intent_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                         "body TEXT NOT NULL, status TEXT NOT NULL, terminal INTEGER NOT NULL, seq INTEGER NOT NULL, "
                         "job TEXT, partial TEXT NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, "
                         "owner TEXT NOT NULL DEFAULT '')")

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _row(row: Tuple) -> dict:
        return {"intent_id": row[0], "kind": row[1], "body": json.loads(row[2]), "status": row[3],
                "terminal": bool(row[4]), "seq": row[5], "job": row[6], "partial": json.loads(row[7]),
                "created_ms": row[8], "updated_ms": row[9], "owner": row[10]}

    _COLS = "intent_id,kind,body,status,terminal,seq,job,partial,created_ms,updated_ms,owner"

    def get(self, intent_id: str) -> Optional[dict]:
        row = self._db.execute(f"SELECT {self._COLS} FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return self._row(row) if row else None

    def live(self) -> List[dict]:
        return [self._row(r) for r in self._db.execute(
            f"SELECT {self._COLS} FROM intents WHERE terminal=0 ORDER BY created_ms")]

    def stats(self) -> dict:
        live, total = self._db.execute("SELECT COALESCE(SUM(terminal=0),0), "
                                       "COALESCE(SUM(length(CAST(body AS BLOB))+length(CAST(partial AS BLOB))),0) "
                                       "FROM intents").fetchone()
        return {"live": live, "bytes": total, "max_live": self.max_live, "max_bytes": self.max_bytes}

    def put(self, record: dict) -> None:
        """Insert or replace one row. Raises LedgerFull rather than exceed a bound."""
        body, partial = json.dumps(record["body"], sort_keys=True), json.dumps(record["partial"], sort_keys=True)
        existing = self._db.execute("SELECT terminal, length(CAST(body AS BLOB))+length(CAST(partial AS BLOB)) "
                                    "FROM intents WHERE intent_id=?",
                                    (record["intent_id"],)).fetchone()
        stats = self.stats()
        grows = len(body) + len(partial) - (existing[1] if existing else 0)
        if existing is None and not record["terminal"] and stats["live"] >= self.max_live:
            raise LedgerFull(f"live intents at their bound ({self.max_live})")
        if grows > 0 and stats["bytes"] + grows > self.max_bytes:
            self.prune()
            if self.stats()["bytes"] + grows > self.max_bytes:
                raise LedgerFull(f"intent ledger at its byte bound ({self.max_bytes})")
        now = self.clock()
        self._db.execute(
            "INSERT INTO intents (intent_id,kind,body,status,terminal,seq,job,partial,created_ms,updated_ms,owner) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET status=excluded.status, "
            "terminal=excluded.terminal, seq=excluded.seq, job=excluded.job, partial=excluded.partial, "
            "updated_ms=excluded.updated_ms",
            (record["intent_id"], record["kind"], body, record["status"], 1 if record["terminal"] else 0,
             record["seq"], record.get("job"), partial, record.get("created_ms", now), now, record.get("owner", "")))

    def prune(self) -> int:
        """Delete terminal rows past their retention. The window is fixed by the catalog, not by env."""
        cutoff = self.clock() - self.retain_terminal_s * 1000
        cur = self._db.execute("DELETE FROM intents WHERE terminal=1 AND updated_ms<?", (cutoff,))
        return cur.rowcount

    def overdue(self) -> List[dict]:
        cutoff = self.clock() - self.expire_after_s * 1000
        return [self._row(r) for r in self._db.execute(
            f"SELECT {self._COLS} FROM intents WHERE terminal=0 AND created_ms<?", (cutoff,))]


# --- the authority -----------------------------------------------------------------------------

Publish = Callable[[dict], Awaitable[Any]]  # sends an `intent_partial` frame (service-minted epoch/seq)


class WorkloadAuthority:
    """Handles `write` frames for harmony.workload/1 and publishes every later rung."""

    def __init__(self, ledger: IntentLedger, backend: Any, publish: Publish, source: str,
                 clock: Optional[Clock] = None, log: Callable[[str], None] = lambda _m: None) -> None:
        self.ledger, self.backend, self.publish, self.source = ledger, backend, publish, source
        self.clock, self.log = optional(clock), log
        self.epoch = 0

    # -- partial minting
    def _partial(self, intent_id: str, seq: int, body: dict) -> dict:
        if self.epoch <= 0 or seq <= 0:
            raise ValueError("workload intent partial needs a positive authority epoch and sequence")
        return {"contract": CONTRACT, "source": self.source, "subject": {"intent_id": intent_id},
                "epoch": self.epoch, "seq": seq, "produced_ms": self.clock(), "ttl_ms": 0, "status": "ok",
                "body": body}

    def _refusal(self, intent_id: str, code: str, detail: str, kind: str = "") -> dict:
        """A refusal that is NOT recorded against an intent (nothing was accepted)."""
        body: Dict[str, Any] = {"intent_id": intent_id, "status": "refused", "refusal": code,
                                "detail": bounded(detail, 256), "kind": bounded(kind or "unknown", 64),
                                "attribution": "infra" if code == REFUSAL_UNAVAILABLE else "workload",
                                "reason": bounded(detail, 240)}
        return self._partial(intent_id, 1, body)

    # -- write handling
    async def handle_write(self, write: Any) -> dict:
        """The `serve` handler: one answer per write frame, never an exception."""
        try:
            if write.op == "submit":
                return await self._submit(write)
            if write.op == "cancel":
                return await self._cancel(write)
            return self._refusal(write.intent_id, REFUSAL_STEP_NOT_ALLOWED, f"{write.op}_is_authority_only")
        except LedgerFull as error:
            self.log(f"workload: refused {write.intent_id}: {error}")
            return self._refusal(write.intent_id, REFUSAL_UNAVAILABLE, f"intent_ledger_full:{error}")

    def _validate(self, write: Any) -> Tuple[Optional[dict], Optional[Tuple[str, str]]]:
        body = (write.payload or {}).get("body")
        if not isinstance(body, dict):
            return None, (REFUSAL_SCHEMA_INVALID, "payload.body must be an object")
        extra = sorted(set(write.payload) - {"body"})
        if extra:
            return None, (REFUSAL_FORBIDDEN_FIELD, f"payload.{extra[0]}")
        if body_size(body) > MAX_BODY_BYTES:
            return None, (REFUSAL_OVERSIZE, f"body exceeds {MAX_BODY_BYTES} bytes; send large inputs by reference")
        for field in sorted(set(body) - _SUBMIT_FIELDS):
            return None, (REFUSAL_FORBIDDEN_FIELD, f"body.{field}")
        if not _INTENT_ID.match(write.intent_id or ""):
            return None, (REFUSAL_SCHEMA_INVALID, "intent_id must be 1..64 of [A-Za-z0-9_.:@-]")
        if body.get("intent_id", write.intent_id) != write.intent_id:
            return None, (REFUSAL_SCHEMA_INVALID, "body.intent_id differs from the frame's intent_id")
        kind = body.get("kind")
        if not isinstance(kind, str) or not 1 <= len(kind) <= 64:
            return None, (REFUSAL_SCHEMA_INVALID, "body.kind must be a 1..64 character string")
        spec = body.get("spec")
        if spec is not None:
            ok = (isinstance(spec, dict) and set(spec) == {"ref", "digest", "bytes", "media"}
                  and isinstance(spec["ref"], str) and 1 <= len(spec["ref"]) <= 512
                  and isinstance(spec["digest"], str) and _DIGEST.match(spec["digest"])
                  and isinstance(spec["bytes"], int) and not isinstance(spec["bytes"], bool) and spec["bytes"] >= 1
                  and isinstance(spec["media"], str) and 1 <= len(spec["media"]) <= 128)
            if not ok:
                return None, (REFUSAL_SCHEMA_INVALID, "body.spec must be a reference {ref,digest,bytes,media}")
        selector = body.get("selector")
        if selector is not None and not (isinstance(selector, dict) and len(selector) <= 16 and all(
                isinstance(k, str) and isinstance(v, str) and len(v) <= 128 for k, v in selector.items())):
            return None, (REFUSAL_SCHEMA_INVALID, "body.selector must map up to 16 names to strings")
        owner = body.get("owner")
        if owner is not None and not (isinstance(owner, str) and len(owner) <= 128):
            return None, (REFUSAL_SCHEMA_INVALID, "body.owner must be a string of at most 128 characters")
        principal = getattr(write, "principal", None)
        if not isinstance(principal, str) or not 1 <= len(principal) <= 128:
            return None, (REFUSAL_SCHEMA_INVALID, "write.principal must name the authenticated requester")
        if owner is not None and owner != principal:
            return None, (REFUSAL_FORBIDDEN_FIELD, "body.owner must match the authenticated requester")
        # Owner is derived from the authenticated write, not caller-selected body data.
        normalized = dict(body)
        normalized.pop("owner", None)
        return normalized, None

    async def _submit(self, write: Any) -> dict:
        body, problem = self._validate(write)
        prior = self.ledger.get(write.intent_id)
        if prior is not None:
            if prior["owner"] != getattr(write, "principal", None):
                return self._refusal(write.intent_id, REFUSAL_FORBIDDEN_FIELD,
                                     "intent_id belongs to another requester", prior["kind"])
            if problem is None and (prior["body"] == body):
                return prior["partial"]  # idempotent: the CURRENT partial, nothing re-run
            return self._refusal(write.intent_id, REFUSAL_SCHEMA_INVALID, "intent_id_reused_with_a_different_body",
                                 prior["kind"])
        if problem is not None:
            self.log(f"workload: refused {write.intent_id}: {problem[0]} {problem[1]}")
            return self._refusal(write.intent_id, problem[0], problem[1], (write.payload or {}).get("body", {}).get("kind", "")
                                 if isinstance((write.payload or {}).get("body"), dict) else "")
        owner = write.principal
        record = {"intent_id": write.intent_id, "kind": body["kind"], "body": body, "status": "submitted",
                  "terminal": False, "seq": 1, "job": None, "created_ms": self.clock(), "owner": owner}
        record["partial"] = self._partial(write.intent_id, 1, {"intent_id": write.intent_id, "status": "submitted",
                                                              "kind": body["kind"]})
        self.ledger.put(record)  # durable BEFORE the job authority is asked: a crash re-announces it
        await self._publish(record["partial"])
        return await self._dispatch(record)

    async def _dispatch(self, record: dict) -> dict:
        """Hand a recorded intent to the job authority (idempotent there on (owner, intent_id))."""
        body = record["body"]
        try:
            job = self.backend.submit(record["owner"], body["kind"], body.get("spec"), body.get("selector") or {},
                                      record["intent_id"])
        except BackendRefused as error:
            attribution = "infra" if error.status in (429, 502, 503, 504) else (
                "workload" if error.status in (400, 403, 404, 409, 413, 422) else "unknown")
            return await self._end(record, "refused", {
                "refusal": REFUSAL_UNAVAILABLE if attribution == "infra" else REFUSAL_SCHEMA_INVALID,
                "detail": bounded(error.detail, 256), "attribution": attribution,
                "reason": bounded(f"job_authority_{error.status}:{error.detail}", 240)})
        except Exception as error:  # noqa: BLE001 - the backend failing is its own named outcome
            self.log(f"workload: backend submit failed for {record['intent_id']}: {error!r}")
            return await self._end(record, "refused", {
                "refusal": REFUSAL_UNAVAILABLE, "detail": bounded(f"backend_error:{error}", 256),
                "attribution": "infra", "reason": bounded(f"backend_error:{type(error).__name__}", 240)})
        record["job"] = job.get("id")
        status, extra = rung_of(job)
        return await self._advance(record, status, extra)

    async def _cancel(self, write: Any) -> dict:
        record = self.ledger.get(write.intent_id)
        if record is None:
            return self._refusal(write.intent_id, REFUSAL_SCHEMA_INVALID, "unknown_intent")
        if record["owner"] != getattr(write, "principal", None):
            return self._refusal(write.intent_id, REFUSAL_FORBIDDEN_FIELD,
                                 "only the authenticated requester may cancel this intent", record["kind"])
        if record["terminal"]:
            return record["partial"]
        if record["status"] not in ("submitted", "admitted"):
            current = json.loads(json.dumps(record["partial"]))
            current["body"]["refusal"] = REFUSAL_STEP_NOT_ALLOWED
            current["body"]["detail"] = "cancel_is_allowed_only_before_placed"
            return current
        try:
            if record["job"]:
                self.backend.cancel(record["owner"], record["job"])
        except Exception as error:  # noqa: BLE001
            self.log(f"workload: cancel of {write.intent_id} failed: {error!r}")
            current = json.loads(json.dumps(record["partial"]))
            current["body"]["refusal"] = REFUSAL_UNAVAILABLE
            current["body"]["detail"] = bounded(f"cancel_failed:{error}", 256)
            return current
        return await self._end(record, "cancelled", {"attribution": "unknown",
                                                     "reason": "cancelled_by_requester_before_placed"})

    # -- rungs
    async def _end(self, record: dict, status: str, fields: Dict[str, Any]) -> dict:
        return await self._advance(record, status, fields)

    async def _advance(self, record: dict, status: str, extra: Dict[str, Any]) -> dict:
        record["seq"] += 1
        body = {"intent_id": record["intent_id"], "status": status, "kind": record["kind"]}
        body.update({k: v for k, v in extra.items() if v is not None})
        if status in TERMINAL_BAD and (body.get("attribution") not in ATTRIBUTIONS or not body.get("reason")):
            body["attribution"], body["reason"] = body.get("attribution") if body.get("attribution") in ATTRIBUTIONS \
                else "unknown", body.get("reason") or "no_reason_given"
        record["status"], record["terminal"] = status, status in TERMINAL
        record["partial"] = self._partial(record["intent_id"], record["seq"], body)
        self.ledger.put(record)
        if status in TERMINAL_BAD:
            self.log(f"workload: {record['intent_id']} {status} attribution={body['attribution']} reason={body['reason']}")
        await self._publish(record["partial"])
        return record["partial"]

    async def _publish(self, partial: dict) -> None:
        try:
            await self.publish(partial)
        except Exception as error:  # noqa: BLE001 - delivery failure is logged, the ledger keeps the truth
            self.log(f"workload: publish of {partial['subject']['intent_id']} failed: {error!r}")

    # -- background
    async def pump(self) -> int:
        """Look at every live intent once; mint a partial for each rung gained. Returns changes."""
        if self.epoch == 0:
            return 0  # not the authority yet (no realm epoch): publishing now would be unversioned
        changed = 0
        order = {name: i for i, name in enumerate(LADDER)}
        live = self.ledger.live()
        job_ids = [record["job"] for record in live if record["job"]]
        try:
            jobs = self.backend.poll_many(job_ids) if job_ids else {}
        except Exception as error:  # noqa: BLE001 - a failed batch read is not evidence that jobs disappeared
            self.log(f"workload: batch poll failed: {error!r}")
            jobs = None
        for record in live:
            if not record["job"]:
                # Crashed between recording and handing off: hand it off now (idempotent on the intent id).
                await self._dispatch(record)
                changed += 1
                continue
            if jobs is None:
                continue
            job = jobs.get(record["job"])
            status, extra = rung_of(job)
            if status in order and order[status] < order[record["status"]]:
                status = record["status"]  # the ladder never regresses (a retry stays `placed`)
            previous = record["partial"]["body"]
            if status == record["status"] and all(previous.get(k) == extra.get(k)
                                                  for k in ("job", "attempt", "worker", "host", "output")):
                continue
            await self._advance(record, status, extra)
            changed += 1
        for record in self.ledger.overdue():
            await self._advance(record, "expired", {"attribution": "unknown", "reason": "unresolved_for_30_days"})
            changed += 1
        self.ledger.prune()
        return changed

    async def reannounce(self, epoch: int) -> int:
        """After (re)gaining the realm authority under `epoch`: publish every non-terminal intent's rung."""
        self.epoch = epoch
        count = 0
        for record in self.ledger.live():
            record["seq"] += 1
            record["partial"] = self._partial(record["intent_id"], record["seq"], record["partial"]["body"])
            self.ledger.put(record)
            await self._publish(record["partial"])
            count += 1
            await asyncio.sleep(REANNOUNCE_PACE_S)
        self.log(f"workload: authority epoch {epoch}; re-announced {count} non-terminal intents")
        return count


class StoreBackend:
    """Adapter over the real WorkloadStore and its owner-scoped CAS.

    `resolve_spec(owner, ref) -> request dict` fetches and digest-verifies the by-reference request.
    The owner-scoped `blobs` store is required; its normal submission input objects are checked before admission.
    """

    def __init__(self, store: Any, resolve_spec: Optional[Callable[[str, dict], dict]] = None,
                 public_base_url: Optional[str] = None, blobs: Any = None) -> None:
        self.store, self.resolve_spec, self.blobs = store, resolve_spec, blobs
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else ""

    def submit(self, owner: str, kind: str, spec: Optional[dict], selector: dict, intent_id: str) -> dict:
        if spec is None:
            raise BackendRefused(400, "spec_reference_required")
        if self.resolve_spec is None:
            raise BackendRefused(503, "spec_resolver_unconfigured")
        from ..workloads.model import WorkloadError  # lazy: the pure modules never need the store
        try:
            if self.blobs is None:
                raise BackendRefused(503, "workload_blob_store_unconfigured")
            resolved = self.resolve_spec(owner, spec)
            if not isinstance(resolved, dict):
                raise BackendRefused(400, "resolved_spec_must_be_an_object")
            request = dict(resolved)
            # The durable intent id is the workload request key. A body cannot split the two idempotency
            # domains by supplying its own key.
            request["key"] = intent_id
            if selector:
                request_selector = request.get("selector", {})
                if not isinstance(request_selector, dict):
                    raise BackendRefused(400, "spec_selector_must_be_an_object")
                request["selector"] = {**request_selector, **selector}
            principal = self.store.principals.get(owner)
            if principal is None:
                raise BackendRefused(403, "workload_principal_not_configured")
            allowed_handlers = principal.handlers
            spec_view = self.store.validate_submission(owner, request, allowed_handlers=allowed_handlers)
            labels = spec_view.get("labels") or {}
            delegated_owner = labels.get("owner")
            if delegated_owner is not None:
                from ..fleet_auth import AuthError, Principal as FleetPrincipal, resolve_owner
                if principal.delegate_prefix is None:
                    raise BackendRefused(403, "principal_cannot_delegate_workload_owner")
                try:
                    resolve_owner(FleetPrincipal(name=principal.id, delegate_prefix=principal.delegate_prefix),
                                  delegated_owner)
                except AuthError as error:
                    raise BackendRefused(error.status, error.detail) from error
            with self.blobs.open(owner, spec_view["input_digest"]):
                pass
            for item in spec_view.get("input_objects", []):
                with self.blobs.open(owner, item["digest"]) as (_, size):
                    if size != item["size"]:
                        raise WorkloadError("input object size mismatch", 409)
            return self.store.submit(owner, request,
                                     allowed_handlers=allowed_handlers)
        except BackendRefused:
            raise
        except WorkloadError as error:
            raise BackendRefused(error.status, str(error)) from error

    def _output_ref(self, job: dict) -> dict:
        """Reference the same bounded completion manifest returned by the owner-authenticated API."""
        _, raw = workload_result_manifest(job)
        job_id = job["id"]
        path = f"/v1/workloads/jobs/{job_id}/result"
        ref = f"{self.public_base_url}{path}" if self.public_base_url else path
        if len(ref) > 512:
            ref = path
        return {"ref": ref, "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "media": "application/json"}

    def poll_many(self, job_ids: List[str]) -> Dict[str, Optional[dict]]:
        """Read a bounded set of job states with one SQLite checkout and one query."""
        ids = list(dict.fromkeys(job_ids))[:MAX_LIVE]
        if not ids:
            return {}
        requested_json = json.dumps(ids, separators=(",", ":"))
        query = """
            WITH requested AS (
                SELECT DISTINCT value AS id FROM json_each(?)
            ), attempt_counts AS (
                SELECT a.job, COUNT(*) AS attempt_count, MAX(a.fence) AS latest_fence
                  FROM attempts a JOIN requested r ON r.id=a.job GROUP BY a.job
            ), latest AS (
                SELECT a.job, a.id, a.worker, a.boot, a.host, a.fence, a.state, a.compilation
                  FROM attempts a JOIN attempt_counts c ON c.job=a.job AND c.latest_fence=a.fence
            ), latest_progress AS (
                SELECT a.job, a.progress,
                       ROW_NUMBER() OVER (PARTITION BY a.job ORDER BY a.fence DESC) AS position
                  FROM attempts a JOIN requested r ON r.id=a.job WHERE a.progress IS NOT NULL
            )
            SELECT j.id, j.owner, j.spec, j.state, j.created, j.updated, j.result, j.cause, j.reason, j.fence,
                   COALESCE(c.attempt_count, 0) AS attempt_count,
                   a.id AS attempt_id, a.worker AS attempt_worker, a.boot AS attempt_boot,
                   a.host AS attempt_host, a.fence AS attempt_fence, a.compilation AS attempt_compilation,
                   a.state AS attempt_state,
                   p.progress AS latest_progress
              FROM requested r JOIN jobs j ON j.id=r.id
              LEFT JOIN attempt_counts c ON c.job=j.id
              LEFT JOIN latest a ON a.job=j.id
              LEFT JOIN latest_progress p ON p.job=j.id AND p.position=1
        """
        with closing(self.store.connect()) as db:
            rows = db.execute(query, (requested_json,)).fetchall()
        result: Dict[str, Optional[dict]] = {job_id: None for job_id in ids}
        for row in rows:
            job: Dict[str, Any] = {"id": row["id"], "owner": row["owner"], "spec": json.loads(row["spec"]),
                                   "state": row["state"], "created": row["created"], "updated": row["updated"],
                                   "result": json.loads(row["result"]) if row["result"] else None,
                                   "cause": json.loads(row["cause"]) if row["cause"] else None,
                                   "reason": row["reason"],
                                   "fence": row["fence"], "attempt_count": row["attempt_count"], "attempts": []}
            if row["attempt_id"] is not None:
                job["attempts"] = [{"id": row["attempt_id"], "worker": row["attempt_worker"],
                                    "boot": row["attempt_boot"], "fence": row["attempt_fence"],
                                    "host": row["attempt_host"], "state": row["attempt_state"],
                                    "compilation": json.loads(row["attempt_compilation"]) if row["attempt_compilation"] else None}]
            if row["latest_progress"]:
                job["progress"] = json.loads(row["latest_progress"])
            if job["state"] == "succeeded" and isinstance(job["result"], dict):
                job["output"] = self._output_ref(job)
            result[job["id"]] = job
        return result

    def recent_jobs(self, limit: int = 256) -> List[dict]:
        """Read a bounded recent job view with one SQLite checkout and one query."""
        limit = max(1, min(int(limit), 256))
        query = """
            WITH selected AS (
                SELECT id FROM jobs ORDER BY updated DESC, id LIMIT ?
            ), attempt_counts AS (
                SELECT a.job, COUNT(*) AS attempt_count
                  FROM attempts a JOIN selected s ON s.id=a.job GROUP BY a.job
            ), live_attempts AS (
                SELECT a.job, a.id, a.worker, a.host, a.state, a.expires, a.created,
                       ROW_NUMBER() OVER (PARTITION BY a.job ORDER BY a.fence DESC) AS position
                  FROM attempts a JOIN selected s ON s.id=a.job WHERE a.state='running'
            ), latest_progress AS (
                SELECT a.job, a.progress,
                       ROW_NUMBER() OVER (PARTITION BY a.job ORDER BY a.fence DESC) AS position
                  FROM attempts a JOIN selected s ON s.id=a.job WHERE a.progress IS NOT NULL
            )
            SELECT j.id, j.owner, j.spec, j.state, j.created, j.updated, j.result, j.cause, j.reason,
                   COALESCE(c.attempt_count, 0) AS attempt_count,
                   a.id AS attempt_id, a.worker AS attempt_worker, a.host AS attempt_host,
                   a.state AS attempt_state, a.expires AS attempt_expires, a.created AS attempt_created,
                   p.progress AS latest_progress
              FROM selected s JOIN jobs j ON j.id=s.id
              LEFT JOIN attempt_counts c ON c.job=j.id
              LEFT JOIN live_attempts a ON a.job=j.id AND a.position=1
              LEFT JOIN latest_progress p ON p.job=j.id AND p.position=1
             ORDER BY j.updated DESC, j.id
        """
        with closing(self.store.connect()) as db:
            rows = db.execute(query, (limit,)).fetchall()
        jobs = []
        for row in rows:
            job: Dict[str, Any] = {"id": row["id"], "owner": row["owner"], "spec": json.loads(row["spec"]),
                                   "state": row["state"], "created": row["created"], "updated": row["updated"],
                                   "result": json.loads(row["result"]) if row["result"] else None,
                                   "cause": json.loads(row["cause"]) if row["cause"] else None,
                                   "reason": row["reason"], "attempt_count": row["attempt_count"], "attempts": []}
            if row["attempt_id"] is not None:
                job["attempts"] = [{"id": row["attempt_id"], "worker": row["attempt_worker"],
                                    "host": row["attempt_host"], "state": row["attempt_state"],
                                    "expires": row["attempt_expires"], "created": row["attempt_created"]}]
            if row["latest_progress"]:
                job["progress"] = json.loads(row["latest_progress"])
            jobs.append(job)
        return jobs

    def cancel(self, owner: str, job_id: str) -> None:
        self.store.cancel(owner, job_id)
