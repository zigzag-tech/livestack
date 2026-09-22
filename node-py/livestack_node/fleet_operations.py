"""fleet_operations.py — the durable half of the fleet broker's control plane.

``fleet_scheduler.schedule()`` is a pure function that says *what should happen*.
Its ``Admit`` actions are already dispatched (``/fleet/admit`` → a lease). Its
``Provision`` and ``Deprovision`` actions were never dispatched by anything, and
this module is why they now can be: **an action that spends money needs a
durable record written before the money is spent, not after.**

The one rule everything here exists to keep: **one logical operation results in
at most one billed create.** Every other design choice follows from it.

* ``creating`` is written to disk BEFORE the provider is called, with the
  idempotency key. A process that dies between the write and the provider's
  reply comes back to that row, moves it to ``uncertain``, and RECONCILES it —
  it never issues a second create. Retrying a create whose outcome is unknown is
  how a fleet rents two machines and pays for both.
* ``uncertain`` is a first-class state with its own record, not an error branch.
  Absence and failure must not look alike: "the provider did not answer" is not
  "the provider said no", and a control plane that cannot tell them apart will
  eventually choose the expensive reading of silence.
* A **claim** is atomic across the owner's own ceiling, every enclosing prefix
  ceiling, the slots already admitted, and the creates already in flight — under
  one writer lock. Checked separately, two concurrent claims at the boundary both
  pass, which is exactly how a naive per-request check is defeated.
* ``announced`` requires a CORRELATED receipt: the node that announced carries
  this ``operation_id``. "A fresh node appeared" is not evidence that *this*
  create succeeded — on a busy fleet something else is always appearing.
* ``released`` requires a drain claim, an empty node, and a final authoritative
  re-check under the writer lock. ``schedule()``'s ``Deprovision`` is a proposal
  computed from a view that is already a few seconds old; it is never a proof.

Bounds (benchday rule 10, the one this repo's ledger already follows): at most
``max_records`` operations, enforced by :meth:`OperationStore.prune` on every
claim, deleting only TERMINAL rows oldest-first. ``max_age_s`` is an additional
window over terminal rows and is **unset by default** — a delete-shaped bound
must not start deleting history on the deploy that introduces it.

Purity, and where it stopped: the two brains stay pure. This is the authority
around them, so it owns a file, a clock and a lock, and it is tested against a
fake provider rather than a mock of one. See
``openspec/specs/fleet-provisioning-operations/`` and
``_plans/fleetd-weave-jev.md``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .fleet_scheduler import SchedulerPolicy, over_quota
from .ledger import Candidate, Decision, new_decision_id

# --- the state machine ------------------------------------------------------
#
# Written as data rather than as `if` branches so that "which transitions exist"
# is a thing a test can enumerate and a reader can see at once. An illegal
# transition RAISES: a control plane that silently ignores one has lost track of
# what it is paying for, and will say so only on the invoice.
INTENT = "intent"
CREATING = "creating"
CREATED = "created"
REJECTED = "rejected"
UNCERTAIN = "uncertain"
ANNOUNCED = "announced"
FAILED = "failed"
RELEASED = "released"

LEGAL_TRANSITIONS: Dict[str, frozenset] = {
    INTENT:    frozenset({CREATING, REJECTED}),
    CREATING:  frozenset({CREATED, UNCERTAIN, REJECTED, FAILED}),
    CREATED:   frozenset({ANNOUNCED, UNCERTAIN, FAILED}),
    UNCERTAIN: frozenset({CREATED, REJECTED, FAILED}),
    ANNOUNCED: frozenset({FAILED, RELEASED}),
    # Terminal. `failed` keeps one exit because an instance that was created and
    # never became usable still has to be torn down, and that teardown is the
    # `released` record — otherwise a failure looks free and it is not.
    FAILED:    frozenset({RELEASED}),
    REJECTED:  frozenset(),
    RELEASED:  frozenset(),
}

#: States in which this operation may still produce a billed instance, or
#: already has one. A claim counts these against quota: an in-flight create is
#: capacity the owner has spent even though no lease exists for it yet.
PENDING_STATES = (INTENT, CREATING, CREATED, UNCERTAIN)
TERMINAL_STATES = (REJECTED, RELEASED)

#: How much of a provider's error text is kept. Enough to recognise the failure
#: class a month later; not enough to turn the store into a log file, and never
#: a credential — see :func:`structured_error`.
MAX_EXCERPT = 400


class OperationError(Exception):
    """Base for the refusals this module states rather than swallows."""


class IllegalTransition(OperationError):
    def __init__(self, operation_id: str, frm: str, to: str):
        super().__init__(f"{operation_id}: {frm} -> {to} is not a legal transition")
        self.operation_id, self.frm, self.to = operation_id, frm, to


class UnknownOperation(OperationError):
    pass


class ClaimRefused(OperationError):
    """The claim was refused, durably and with a reason. ``operation`` is the
    recorded ``rejected`` row — a refusal is an ANSWER, and an answer that leaves
    nothing behind cannot be argued with later."""

    def __init__(self, operation: "Operation"):
        super().__init__(operation.reason or "claim refused")
        self.operation = operation


class _Recheck(Exception):
    """Internal: the final busy re-check fired inside the writer transaction, so
    the whole transaction — drain claim included — has to roll back."""


class DrainRefused(OperationError):
    """A release was refused because the node was not empty. Carries the reason
    the busy-check gave, so `busy` never has to be inferred from a stack trace."""

    def __init__(self, operation_id: str, reason: str):
        super().__init__(f"{operation_id}: {reason}")
        self.operation_id, self.reason = operation_id, reason


def structured_error(stage: str, cls: str, code: str,
                     excerpt: str = "") -> Dict[str, str]:
    """``{stage, class, code, excerpt}`` — the shape the supervision loop keys its
    registered-workflow table on.

    The point of the CODE is that a known failure never has to be diagnosed: the
    loop looks it up and runs the workflow. The point of the bounded EXCERPT is
    that an unknown one can still be read by a person. A provider message is
    truncated, never parsed for meaning here.
    """
    return {"stage": stage, "class": cls, "code": code,
            "excerpt": str(excerpt or "")[:MAX_EXCERPT]}


@dataclass(frozen=True)
class Operation:
    """One provisioning operation, as recorded. Values, not references — the same
    discipline the decision ledger follows, and for the same reason: a row that
    points at a view which has since changed cannot settle an argument."""
    operation_id: str
    idempotency_key: str
    job_id: str
    kind: str
    owner: str
    target_id: str
    state: str
    created_at: float
    updated_at: float
    principal: Optional[str] = None
    tier: Optional[str] = None
    provider: Optional[str] = None
    region: Optional[str] = None
    plan_version: Optional[str] = None
    announce_deadline: Optional[float] = None
    provider_instance_id: Optional[str] = None
    #: The fleet node this operation produced, once one announced carrying its
    #: id. Recorded because it is the join a drain needs: `Deprovision` names a
    #: node, and the operation that paid for that node is the thing that has to
    #: reach `released`.
    node_id: Optional[str] = None
    error: Optional[Dict[str, str]] = None
    reason: Optional[str] = None
    #: A ledger record for one of this operation's transitions could not be
    #: written. The transition still applied — observability is never worth
    #: failing a paid operation for — but the row says so, and `GET /fleet`
    #: reports it, because a silent gap in the audit trail is indistinguishable
    #: from an audit trail that was never needed.
    observability_degraded: bool = False
    drain_claimed_at: Optional[float] = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["terminal"] = self.terminal
        return d


_COLUMNS = ("operation_id", "idempotency_key", "job_id", "kind", "owner", "principal",
            "target_id", "tier", "provider", "region", "plan_version", "state",
            "created_at", "updated_at", "announce_deadline", "provider_instance_id",
            "node_id", "error", "reason", "observability_degraded", "drain_claimed_at")

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT PRIMARY KEY,
 idempotency_key TEXT NOT NULL UNIQUE,
 job_id TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT '',
 owner TEXT NOT NULL,
 principal TEXT,
 target_id TEXT NOT NULL,
 tier TEXT,
 provider TEXT,
 region TEXT,
 plan_version TEXT,
 state TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 announce_deadline REAL,
 provider_instance_id TEXT,
 node_id TEXT,
 error TEXT,
 reason TEXT,
 observability_degraded INTEGER NOT NULL DEFAULT 0,
 drain_claimed_at REAL
);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state, created_at);
CREATE INDEX IF NOT EXISTS operations_owner ON operations(owner, state);
CREATE INDEX IF NOT EXISTS operations_job ON operations(job_id);
CREATE INDEX IF NOT EXISTS operations_node ON operations(node_id, state);
CREATE TABLE IF NOT EXISTS drains (
 target_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL,
 claimed_at REAL NOT NULL
);
"""


def _row_to_operation(row: sqlite3.Row) -> Operation:
    d = {k: row[k] for k in _COLUMNS}
    d["error"] = json.loads(d["error"]) if d["error"] else None
    d["observability_degraded"] = bool(d["observability_degraded"])
    return Operation(**d)


@dataclass
class _Emission:
    """A ledger record computed inside a transaction and written after it commits.

    Deliberate: the durable state is the operation store, and the ledger is the
    retrospective. Writing the retrospective inside the writer lock would let a
    slow disk hold the claim path, and letting a ledger failure roll back a
    transition would make observability able to refuse a paid operation —
    exactly backwards.
    """
    operation_id: str
    decision: Decision


class OperationStore:
    """The durable operation authority. Every transition is a SQLite transaction.

    One writer lock (``BEGIN IMMEDIATE``) serialises claims, which is what makes
    the quota check atomic against concurrent callers. Readers do not take it.
    """

    def __init__(self, path: str, *, ledger=None, emitter: str = "fleet-broker",
                 emitter_id: str = "fleet-broker", clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda *_: None,
                 max_records: int = 5000, max_age_s: Optional[float] = None):
        self.path = str(path)
        self.ledger = ledger
        self.emitter = emitter
        self.emitter_id = emitter_id
        self._clock = clock
        self._log = log
        self.max_records = max(1, int(max_records))
        self.max_age_s = max_age_s
        d = os.path.dirname(self.path)
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA journal_size_limit=16777216")
            db.executescript(SCHEMA)

    # -- plumbing ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _writer(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else now

    # -- reading -------------------------------------------------------------
    def get(self, operation_id: str) -> Optional[Operation]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
        return _row_to_operation(row) if row else None

    def by_idempotency_key(self, key: str) -> Optional[Operation]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM operations WHERE idempotency_key=?",
                             (key,)).fetchone()
        return _row_to_operation(row) if row else None

    def for_job(self, job_id: str) -> List[Operation]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE job_id=? ORDER BY created_at", (job_id,)
            ).fetchall()
        return [_row_to_operation(r) for r in rows]

    def for_node(self, node_id: str) -> Optional[Operation]:
        """The live operation that paid for this node, if the fleet broker
        provisioned it. `None` for a node nobody here created — which is most of
        them, and which is why a deprovision of one is refused rather than
        attempted."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE node_id=? AND state NOT IN (?,?)"
                " ORDER BY created_at DESC LIMIT 1",
                (node_id,) + TERMINAL_STATES).fetchone()
        return _row_to_operation(row) if row else None

    def active(self) -> List[Operation]:
        """Everything that is not terminal — what a supervision tick supervises."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE state NOT IN (?,?) ORDER BY created_at",
                TERMINAL_STATES).fetchall()
        return [_row_to_operation(r) for r in rows]

    def pending_usage(self) -> Dict[str, int]:
        """Creates in flight per owner. Exposed because the plan has to show them:
        capacity an owner has already committed to is not free capacity, and a
        planner that cannot see it will hand out the same room twice."""
        with closing(self._connect()) as db:
            return self._pending_usage(db)

    @staticmethod
    def _pending_usage(db: sqlite3.Connection) -> Dict[str, int]:
        q = ("SELECT owner, COUNT(*) AS n FROM operations WHERE state IN "
             "(?,?,?,?) GROUP BY owner")
        return {r["owner"]: int(r["n"]) for r in db.execute(q, PENDING_STATES)}

    # -- claiming ------------------------------------------------------------
    def claim(self, *, job_id: str, owner: str, target_id: str,
              idempotency_key: str, kind: str = "", principal: Optional[str] = None,
              tier: Optional[str] = None, provider: Optional[str] = None,
              region: Optional[str] = None, plan_version: Optional[str] = None,
              usage: Optional[Mapping[str, int]] = None,
              policy: Optional[SchedulerPolicy] = None,
              allow_regions: Optional[Tuple[str, ...]] = None,
              allow_unknown_region: bool = False,
              announce_deadline_s: float = 900.0,
              now: Optional[float] = None) -> Operation:
        """Reserve the right to create one instance, atomically. Returns the
        operation in ``creating``; raises :class:`ClaimRefused` otherwise.

        **Idempotent by key.** Handed a key it has already seen, it returns that
        operation unchanged rather than claiming again — so a caller that retries
        a request whose reply it never received cannot double-book, and neither
        can two callers that agreed on a key.

        Everything the refusal turns on is read INSIDE the writer transaction:
        ``usage`` (the broker's live lease ledger) is the only value passed in,
        and it is combined with the in-flight creates this store knows about
        before ``over_quota`` sees it. Two claims racing at the boundary
        therefore serialise, and exactly one of them wins.
        """
        now = self._now(now)
        pol = policy or SchedulerPolicy()
        emissions: List[_Emission] = []
        with self._writer() as db:
            existing = db.execute("SELECT * FROM operations WHERE idempotency_key=?",
                                  (idempotency_key,)).fetchone()
            if existing is not None:
                op = _row_to_operation(existing)
                if op.state == REJECTED:
                    raise ClaimRefused(op)
                return op

            operation_id = new_decision_id(now)
            op = Operation(
                operation_id=operation_id, idempotency_key=idempotency_key,
                job_id=job_id, kind=kind, owner=owner, principal=principal,
                target_id=target_id, tier=tier, provider=provider, region=region,
                plan_version=plan_version, state=INTENT,
                created_at=now, updated_at=now,
                announce_deadline=now + max(0.0, announce_deadline_s))
            # The refusal is computed BEFORE the row exists, so that an
            # operation cannot count itself against its owner's ceiling — which
            # it would, since `intent` is one of the states a pending create is
            # counted in, and every ceiling would then be one lower than it says.
            refusal = self._refusal(db, owner=owner, region=region, usage=usage,
                                    policy=pol, allow_regions=allow_regions,
                                    allow_unknown_region=allow_unknown_region)
            self._insert(db, op)
            emissions.append(self._record(op, "claim", f"claim {job_id} -> {target_id}"))
            to = REJECTED if refusal else CREATING
            op = self._apply(db, op, to, now, reason=refusal)
            emissions.append(self._record(
                op, "claim",
                refusal or f"claim accepted: {target_id} reserved for {job_id}"))
            self._prune(db, now)
        self._flush(emissions)
        if refusal:
            raise ClaimRefused(self.get(op.operation_id) or op)
        return self.get(op.operation_id) or op

    def _refusal(self, db, *, owner, region, usage, policy,
                 allow_regions, allow_unknown_region) -> Optional[str]:
        """Why this claim may not proceed, or None. Region first, then quota.

        Region is checked HERE and not only where the plan was computed, because
        the plan was computed from a view and the dispatch is the thing that
        spends: the filter that guarantees the answer has to sit on the path that
        acts. `hostd` applies the same policy before scheduling; this is the
        second half of that guarantee, not a duplicate of it.
        """
        if allow_regions:
            allowed = {str(r).strip().lower() for r in allow_regions if str(r).strip()}
            here = (region or "").strip().lower()
            if not here:
                if not allow_unknown_region:
                    return ("region: target reports no region and the caller did "
                            f"not allow unknown (wanted {'/'.join(sorted(allowed))})")
            elif here not in allowed:
                return (f"region: target is in {here}, caller allows "
                        f"{'/'.join(sorted(allowed))}")
        # Slots already held (leases) PLUS creates already in flight. Counting
        # only the first is how an owner at its ceiling gets four more machines
        # by asking for them before any of them announce.
        combined: Dict[str, int] = dict(usage or {})
        for o, n in self._pending_usage(db).items():
            combined[o] = combined.get(o, 0) + n
        return over_quota(owner, combined, policy)

    # -- transitions ---------------------------------------------------------
    def transition(self, operation_id: str, to_state: str, *,
                   reason: Optional[str] = None,
                   error: Optional[Mapping[str, str]] = None,
                   provider_instance_id: Optional[str] = None,
                   node_id: Optional[str] = None,
                   now: Optional[float] = None) -> Operation:
        """Advance one operation. Raises :class:`IllegalTransition` rather than
        ignoring a move the machine does not have."""
        now = self._now(now)
        with self._writer() as db:
            op = self._load(db, operation_id)
            op = self._apply(db, op, to_state, now, reason=reason, error=error,
                             provider_instance_id=provider_instance_id,
                             node_id=node_id)
            emission = self._record(op, "operation",
                                    reason or f"{operation_id} -> {to_state}")
        self._flush([emission])
        return self.get(operation_id) or op

    def _apply(self, db, op: Operation, to_state: str, now: float, *,
               reason: Optional[str] = None,
               error: Optional[Mapping[str, str]] = None,
               provider_instance_id: Optional[str] = None,
               node_id: Optional[str] = None,
               drain_claimed_at: Optional[float] = None) -> Operation:
        if to_state not in LEGAL_TRANSITIONS.get(op.state, frozenset()):
            raise IllegalTransition(op.operation_id, op.state, to_state)
        nxt = replace(
            op, state=to_state, updated_at=now,
            reason=reason if reason is not None else op.reason,
            error=dict(error) if error is not None else op.error,
            provider_instance_id=(provider_instance_id
                                  if provider_instance_id is not None
                                  else op.provider_instance_id),
            node_id=node_id if node_id is not None else op.node_id,
            drain_claimed_at=(drain_claimed_at if drain_claimed_at is not None
                              else op.drain_claimed_at))
        db.execute(
            "UPDATE operations SET state=?, updated_at=?, reason=?, error=?,"
            " provider_instance_id=?, node_id=?, drain_claimed_at=?"
            " WHERE operation_id=?",
            (nxt.state, nxt.updated_at, nxt.reason,
             json.dumps(nxt.error) if nxt.error else None,
             nxt.provider_instance_id, nxt.node_id, nxt.drain_claimed_at,
             nxt.operation_id))
        # Unconditional, with the condition in the message: a line that appears
        # only on trouble is a line whose silence proves nothing.
        self._log(f"[operations] {nxt.operation_id} {op.state} -> {nxt.state}"
                  + (f": {nxt.reason}" if nxt.reason else ""))
        return nxt

    @staticmethod
    def _load(db, operation_id: str) -> Operation:
        row = db.execute("SELECT * FROM operations WHERE operation_id=?",
                         (operation_id,)).fetchone()
        if row is None:
            raise UnknownOperation(operation_id)
        return _row_to_operation(row)

    @staticmethod
    def _insert(db, op: Operation) -> None:
        db.execute(
            f"INSERT INTO operations ({','.join(_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in _COLUMNS)})",
            (op.operation_id, op.idempotency_key, op.job_id, op.kind, op.owner,
             op.principal, op.target_id, op.tier, op.provider, op.region,
             op.plan_version, op.state, op.created_at, op.updated_at,
             op.announce_deadline, op.provider_instance_id, op.node_id,
             json.dumps(op.error) if op.error else None, op.reason,
             int(op.observability_degraded), op.drain_claimed_at))

    # -- restart, reconciliation, correlation --------------------------------
    def recover(self, now: Optional[float] = None) -> List[Operation]:
        """Called at startup, BEFORE any new create. Every operation caught mid-
        create becomes ``uncertain``.

        It does not resolve them — resolving needs the provider, and a provider
        that is also down at startup must not turn into "assume nothing was
        created". It marks them, so that the one thing which must never happen
        (issuing a second create for an unresolved first one) cannot happen by
        default: :meth:`claim` is the only path to a create, and an ``uncertain``
        row already holds that owner's quota.
        """
        now = self._now(now)
        out: List[Operation] = []
        emissions: List[_Emission] = []
        with self._writer() as db:
            rows = db.execute("SELECT * FROM operations WHERE state=?",
                              (CREATING,)).fetchall()
            for row in rows:
                op = self._apply(
                    db, _row_to_operation(row), UNCERTAIN, now,
                    reason="restart while creating: the provider's reply, if any, was lost",
                    error=structured_error("create", "uncertain_effect", "restart"))
                out.append(op)
                emissions.append(self._record(op, "operation", op.reason or ""))
        self._flush(emissions)
        if out:
            self._log(f"[operations] restart: {len(out)} operation(s) marked uncertain; "
                      f"each needs reconcile before any new create")
        else:
            self._log("[operations] restart: no operation was mid-create")
        return out

    def reconcile(self, operation_id: str,
                  lookup: Callable[[str], Optional[str]],
                  now: Optional[float] = None) -> Operation:
        """Resolve an unresolved create by ASKING the provider, never by retrying.

        ``lookup(idempotency_key)`` returns the provider's instance id if that
        key produced one, or None if it provably did not. A lookup that cannot
        answer must RAISE rather than return None — "I could not check" and
        "nothing was created" are the two readings this whole state exists to
        keep apart, and collapsing them here would undo it.
        """
        op = self.get(operation_id)
        if op is None:
            raise UnknownOperation(operation_id)
        if op.state not in (CREATING, UNCERTAIN):
            return op
        instance_id = lookup(op.idempotency_key)
        if instance_id:
            return self.transition(
                operation_id, CREATED, provider_instance_id=instance_id,
                reason=f"reconciled: provider holds {instance_id} for this key",
                error=None, now=now)
        return self.transition(
            operation_id, REJECTED,
            reason="reconciled: the provider has no instance for this key; nothing was billed",
            now=now)

    def announce(self, operation_id: str, *, ready: bool,
                 node: Optional[str] = None,
                 now: Optional[float] = None) -> Optional[Operation]:
        """A node carrying ``operation_id`` announced. Green only when it is also
        ``ready``.

        Correlation is the whole point. The node states the id it was created
        with, so an unrelated node becoming fresh during the operation — which on
        a live fleet happens constantly — cannot green it. An id this store does
        not know returns None rather than raising: a stale instance announcing
        after its operation was pruned is not an error, it is old news.
        """
        op = self.get(operation_id)
        if op is None:
            return None
        if op.state != CREATED:
            return op
        if not ready:
            return op
        return self.transition(
            operation_id, ANNOUNCED, node_id=node,
            reason=f"announced by {node or 'the node'} carrying this operation_id, ready",
            now=now)

    def expire(self, now: Optional[float] = None) -> List[Operation]:
        """Operations that ran out of deadline. ``created`` past its announce
        deadline FAILS — an instance that never became usable is still billed, so
        it has to become visible rather than sit in the store looking busy."""
        now = self._now(now)
        out: List[Operation] = []
        emissions: List[_Emission] = []
        with self._writer() as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE state=? AND announce_deadline IS NOT NULL"
                " AND announce_deadline < ?", (CREATED, now)).fetchall()
            for row in rows:
                op = self._apply(
                    db, _row_to_operation(row), FAILED, now,
                    reason="created but never announced within the deadline",
                    error=structured_error("announce", "provider_fault", "never_announced"))
                out.append(op)
                emissions.append(self._record(op, "operation", op.reason or ""))
        self._flush(emissions)
        return out

    # -- draining ------------------------------------------------------------
    def release(self, operation_id: str, *,
                busy: Callable[[], Optional[str]],
                now: Optional[float] = None) -> Operation:
        """Give a worker back. Four gates, in this order, and the last one is the
        one that matters.

        ``busy()`` returns a sentence when the node still holds a lease, a job,
        or a pending admission, and None when it is empty. It is called twice:
        once cheaply, and once again UNDER the writer lock as the final
        authoritative re-check. The second call is not belt-and-braces — the
        plan that proposed this deprovision was computed from a view, and a job
        can be admitted to the node in the seconds between the view and the act.
        A refusal is recorded; releasing a busy worker kills running work.
        """
        now = self._now(now)
        op = self.get(operation_id)
        if op is None:
            raise UnknownOperation(operation_id)
        reason = busy()
        if reason:
            self._flush([self._record(op, "operation", f"release refused: {reason}",
                                      outcome_status="failed")])
            raise DrainRefused(operation_id, reason)
        recheck: Optional[str] = None
        try:
            with self._writer() as db:
                op = self._load(db, operation_id)
                db.execute(
                    "INSERT OR REPLACE INTO drains (target_id, operation_id, claimed_at)"
                    " VALUES (?,?,?)", (op.target_id, op.operation_id, now))
                recheck = busy()
                if recheck:
                    # Raise, so the drain claim rolls back with everything else:
                    # a claim that outlived its refusal would block the next
                    # honest attempt to drain the same node.
                    raise _Recheck(recheck)
                op = self._apply(db, op, RELEASED, now,
                                 reason="drained: no lease, no job, no pending admission",
                                 drain_claimed_at=now)
                emission = self._record(op, "operation", op.reason or "")
        except _Recheck:
            self._flush([self._record(op, "operation",
                                      f"release refused on re-check: {recheck}",
                                      outcome_status="failed")])
            raise DrainRefused(operation_id, recheck or "busy")
        self._flush([emission])
        return self.get(operation_id) or op

    def drain_claimed(self, target_id: str) -> Optional[float]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT claimed_at FROM drains WHERE target_id=?",
                             (target_id,)).fetchone()
        return float(row["claimed_at"]) if row else None

    # -- bounds --------------------------------------------------------------
    def _prune(self, db, now: float) -> int:
        """Enforce the bound. Only TERMINAL rows are ever deleted: a pending
        create is the thing this store exists to remember, and a bound that could
        forget one would trade a bounded disk for an unbounded bill."""
        gone = 0
        if self.max_age_s is not None:
            cur = db.execute(
                "DELETE FROM operations WHERE state IN (?,?) AND updated_at < ?",
                TERMINAL_STATES + (now - self.max_age_s,))
            gone += cur.rowcount or 0
        total = db.execute("SELECT COUNT(*) AS n FROM operations").fetchone()["n"]
        over = int(total) - self.max_records
        if over > 0:
            cur = db.execute(
                "DELETE FROM operations WHERE operation_id IN ("
                " SELECT operation_id FROM operations WHERE state IN (?,?)"
                " ORDER BY updated_at LIMIT ?)", TERMINAL_STATES + (over,))
            gone += cur.rowcount or 0
        db.execute("DELETE FROM drains WHERE operation_id NOT IN"
                   " (SELECT operation_id FROM operations)")
        if gone:
            self._log(f"[operations] pruned {gone} terminal record(s); bound is "
                      f"{self.max_records} records"
                      + (f" and {self.max_age_s:.0f}s" if self.max_age_s else
                         " (age window unset = disabled)"))
        return gone

    # -- the retrospective ---------------------------------------------------
    def _record(self, op: Operation, decision: str, reason: str,
                outcome_status: str = "ok") -> _Emission:
        """One ledger record per transition and per claim outcome.

        The candidate row is the target, with the outcome that actually befell
        it — `filtered` for a refusal, `chosen` otherwise — so an operator
        reading the ledger sees refusals in the same shape as placements rather
        than having to know that refusals live somewhere else.
        """
        filtered = op.state in (REJECTED,) or outcome_status != "ok"
        return _Emission(op.operation_id, Decision(
            emitter=self.emitter, emitter_id=self.emitter_id,
            kind=op.kind or None, decision=decision,
            candidates=[Candidate(
                id=op.target_id,
                outcome="filtered" if filtered else "chosen",
                rank=None if filtered else 1,
                reason=reason or f"{op.state}",
                region=op.region, inputs_at=op.updated_at)],
            chosen=None if filtered else op.target_id,
            reason=reason,
            request={"owner": op.owner, "principal": op.principal,
                     "job_id": op.job_id, "operation_id": op.operation_id,
                     "idempotency_key": op.idempotency_key,
                     "tier": op.tier, "provider": op.provider,
                     "plan_version": op.plan_version},
            dispatched=op.state in (CREATING, CREATED, ANNOUNCED, RELEASED),
            outcome={"status": outcome_status, "recorded_at": op.updated_at,
                     "state": op.state,
                     "provider_instance_id": op.provider_instance_id,
                     "error": op.error},
        ))

    def _flush(self, emissions: List[_Emission]) -> None:
        """Write the records, and mark the operation when one could not be
        written. The transition already applied — observability must never be
        able to refuse a paid operation — but a gap in the audit trail is
        reported rather than discovered, because an empty ledger and a broken
        ledger look identical from the outside."""
        if self.ledger is None:
            return
        for e in emissions:
            if self.ledger.append(e.decision) is None:
                self._mark_degraded(e.operation_id)

    def _mark_degraded(self, operation_id: str) -> None:
        try:
            with self._writer() as db:
                db.execute("UPDATE operations SET observability_degraded=1"
                           " WHERE operation_id=?", (operation_id,))
        except Exception as exc:  # noqa: BLE001 — the store is the thing that broke
            self._log(f"[operations] {operation_id}: ledger write failed AND the "
                      f"degraded flag could not be set: {exc}")
            return
        self._log(f"[operations] {operation_id}: ledger write failed; operation is "
                  f"observability_degraded (the transition still applied)")


def store_from_env(env: Optional[Mapping[str, str]] = None, *, ledger=None,
                   emitter_id: str = "fleet-broker",
                   log: Callable[[str], None] = lambda *_: None) -> OperationStore:
    """The store the fleet broker runs with. Lives beside the decision ledger
    (``LIVESTACK_LEDGER_DIR``), because the two are read together and an operator
    who found one has found the other."""
    env = os.environ if env is None else env
    d = env.get("LIVESTACK_LEDGER_DIR") or os.path.expanduser("~/.cache/livestack")
    age = str(env.get("LIVESTACK_OPERATIONS_AGE_DAYS", "")).strip()
    return OperationStore(
        os.path.join(d, "fleet-operations.sqlite3"),
        ledger=ledger, emitter_id=emitter_id, log=log,
        max_records=int(env.get("LIVESTACK_OPERATIONS_MAX", "5000")),
        max_age_s=float(age) * 86400 if age else None)
