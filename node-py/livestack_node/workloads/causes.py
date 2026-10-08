"""Typed terminal causes (openspec typed-outcome-causes-and-blockers).

Every terminal job carries `cause: {kind, retry, evidence}`. `kind` comes from a closed list and
`retry` is the authority's advice to the caller (`same | elsewhere | after_change | no`). The
authority derives the cause from what the completion actually carries, so no worker change is needed
for the kinds it can already see; a worker may add a `cause_kind` hint (validated, coerced to
`unknown` when this authority does not know the name) and the kernel/systemd evidence in `resources`.

`unknown` is an explicit value that names what could not be read. It is never defaulted to
infrastructure and never guessed to be an OOM kill.
"""
from __future__ import annotations

from .model import encode

CAUSE_BYTES = 2048

# kind -> default retry advice.
KINDS = {
    "succeeded": "no",
    "handler_failed": "no",
    "oom_killed": "after_change",
    "pids_exhausted": "after_change",
    "wall_time_exceeded": "after_change",
    "disk_exhausted": "after_change",
    "lease_lost": "elsewhere",
    "worker_lost": "elsewhere",
    "capability_absent": "elsewhere",
    "resource_exhausted": "elsewhere",
    "unplaceable": "after_change",
    "stalled_no_progress": "elsewhere",
    "deadline_expired": "no",
    "scope_closed": "no",
    "cancelled_by_owner": "no",
    "unknown": "elsewhere",
}
RETRY = ("same", "elsewhere", "after_change", "no")
# The ledger's `resource_limit` kinds (store._limit_breach) mapped onto this vocabulary.
BREACH_KINDS = {"memory": "oom_killed", "tasks": "pids_exhausted", "disk": "disk_exhausted"}


def _text(value, limit=160):
    return str(value)[:limit]


def make(kind, evidence=None, *, retry=None, fence=None, attempts=None):
    """A validated cause. `unknown` stops advising a retry once the attempts are used up."""
    if kind not in KINDS:
        raise ValueError("unknown cause kind %r" % (kind,))
    advice = retry or KINDS[kind]
    if kind == "unknown" and fence is not None and attempts is not None and fence >= attempts:
        advice = "no"
    cause = {"kind": kind, "retry": advice, "evidence": dict(evidence or {})}
    # Evidence is bounded by dropping the largest value until the document fits, then stating the loss.
    while len(encode(cause, 1 << 20).encode()) > CAUSE_BYTES and cause["evidence"]:
        biggest = max(cause["evidence"], key=lambda k: len(str(cause["evidence"][k])))
        del cause["evidence"][biggest]
        cause["evidence"]["truncated"] = True
    return cause


def coerce_hint(value):
    """A worker-reported kind this authority does not know becomes `unknown`, original name kept (40 chars)."""
    if isinstance(value, str) and value in KINDS:
        return value, {}
    return "unknown", {"reported_kind": _text(value, 40)}


def derive(outcome, result, *, breach=None, fence=None, attempts=None):
    """The cause of a completion the worker reported. Never raises: a classifier fault is `unknown`."""
    try:
        return _derive(outcome, result if isinstance(result, dict) else {}, breach, fence, attempts)
    except Exception as error:  # noqa: BLE001 - classification must not lose the completion
        return make("unknown", {"classifier_error": _text(type(error).__name__ + ": " + str(error))},
                    fence=fence, attempts=attempts)


def _derive(outcome, result, breach, fence, attempts):
    if outcome == "succeeded":
        return make("succeeded")
    if outcome == "product_failure":
        return make("handler_failed", {"exit_code": result.get("exit_code")})
    resources = result.get("resources") if isinstance(result.get("resources"), dict) else {}
    if breach:
        evidence = {"source": breach.get("source"), "observed": breach.get("observed"),
                    "declared": breach.get("declared")}
        return make(BREACH_KINDS[breach["kind"]], {k: v for k, v in evidence.items() if v is not None})
    hint = result.get("cause_kind")
    if hint is not None:
        kind, extra = coerce_hint(hint)
        return make(kind, extra, fence=fence, attempts=attempts)
    error, detail = result.get("error"), str(result.get("detail") or "")
    if resources.get("unit_result") == "timeout":
        return make("wall_time_exceeded", {"source": "systemd", "unit_result": "timeout"})
    if detail.startswith("execution lease lost"):
        return make("lease_lost", {"detail": _text(detail)})
    if error == "abandoned" and detail.startswith("execution lease expired"):
        return make("worker_lost", {"detail": _text(detail)})
    unreadable = ["receipt"]
    if resources.get("resource_evidence") == "none" or not resources:
        unreadable += ["memory.events", "pids.events", "unit properties"]
    evidence = {"unreadable": unreadable}
    for key in ("error", "detail", "exit_code"):
        if result.get(key) is not None:
            evidence[key] = _text(result[key])
    for key in ("unit_result", "exec_main_status"):
        if resources.get(key) is not None:
            evidence[key] = _text(resources[key], 40)
    return make("unknown", evidence, fence=fence, attempts=attempts)


def validate_stored(value):
    """Used by the migration test and views: a stored cause has the closed shape."""
    return (isinstance(value, dict) and set(value) == {"kind", "retry", "evidence"}
            and value["kind"] in KINDS and value["retry"] in RETRY and isinstance(value["evidence"], dict))
