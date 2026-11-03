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

import json

from .log_excerpt import clean as clean_excerpt
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
    # The authority-wide wall-clock bounds (Limits.max_attempt_seconds / max_queue_age_seconds): the job
    # itself is too large or too long-lived for shared capacity. Re-running it unchanged fails the same way.
    "attempt_wall_clock_exceeded": "after_change",
    "queue_age_exceeded": "after_change",
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


def _excerpt(result):
    found = clean_excerpt(result.get("log_excerpt"))
    return {"excerpt": found} if found else {}


def reason_of(cause):
    """Return the bounded reason supported by a terminal cause, if any."""
    evidence = cause.get("evidence") if isinstance(cause, dict) else None
    if (isinstance(evidence, dict) and cause.get("kind") == "succeeded" and
            all(type(evidence.get(key)) is int and evidence[key] == 0
                for key in ("exit_code", "oom_kill", "pids_max_events"))):
        return "handler_exit_code_zero"
    if isinstance(cause, dict) and cause.get("kind") == "handler_failed":
        found = (cause.get("evidence") or {}).get("excerpt")
        if found:
            return "handler failed: " + found
    return None


def limit_breach(result, need):
    """Return the store's typed resource-limit breach, shared with worker classification."""
    resources = result.get("resources") if isinstance(result, dict) else None
    resources = resources if isinstance(resources, dict) else {}
    try:
        need = json.loads(need) if isinstance(need, str) else (need or {})
    except (TypeError, ValueError):
        need = {}
    if not isinstance(need, dict):
        need = {}

    def breach(kind, observed, declared, detail):
        return {"cause": "resource_limit", "kind": kind, "observed": observed, "declared": declared,
                "retryable": False, "source": resources.get("source"), "detail": detail}

    oom_kill = resources.get("oom_kill")
    if isinstance(oom_kill, (int, float)) and oom_kill > 0:
        declared = need.get("memory_bytes", need.get("ram"))
        observed = resources.get("memory_peak_bytes")
        return breach("memory", observed, declared,
                      "resource limit: memory peak %s bytes reached the job's declared need.memory_bytes %s; "
                      "raise the job's need, a retry would hit the same cap"
                      % (observed if observed is not None else "unknown",
                         declared if declared is not None else "unknown"))
    pids_max_events = resources.get("pids_max_events")
    if isinstance(pids_max_events, (int, float)) and pids_max_events > 0:
        observed = resources.get("tasks_peak")
        return breach("tasks", observed, None,
                      "resource limit: the attempt reached its task limit (peak %s); raise the handler's max_tasks, "
                      "a retry would hit the same cap" % (observed if observed is not None else "unknown"))
    declared, grown = need.get("disk_bytes"), resources.get("disk_delta_bytes")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool) and isinstance(grown, int) and \
            not isinstance(grown, bool) and grown >= declared:
        return breach("disk", grown, declared,
                      "resource limit: the workspace filesystem grew by %s bytes, reaching the job's declared "
                      "need.disk_bytes %s; raise the job's need, a retry would hit the same cap" % (grown, declared))
    return None


def success_metadata(result, need=None):
    """The bounded terminal classification supported by an explicit clean worker receipt.

    Missing measurements are not zero. This is also used by readers of historical
    records so they can distinguish a verified success from a state-only legacy row.
    """
    if not isinstance(result, dict) or type(result.get("exit_code")) is not int or result["exit_code"] != 0:
        return None
    resources = result.get("resources")
    if not isinstance(resources, dict) or resources.get("resource_evidence") == "none":
        return None
    if need is not None and limit_breach(result, need) is not None:
        return None
    counters = {}
    for key in ("oom_kill", "pids_max_events"):
        value = resources.get(key)
        if type(value) is not int or value < 0 or value != 0:
            return None
        counters[key] = value
    evidence = {"exit_code": 0, **counters}
    cause = make("succeeded", evidence)
    return {"attribution": "workload", "reason": "handler_exit_code_zero", "cause": cause}


def completion_success_metadata(completion, need=None):
    """Read success metadata from a stored completion, deriving only from its receipt."""
    if not isinstance(completion, dict):
        completion = {}
    result = completion.get("result")
    metadata = success_metadata(result, need=need)
    if metadata is None:
        return {"attribution": "unknown", "reason": "success_evidence_unavailable",
                "cause": make("unknown", {"unreadable": ["exit_code", "resources.oom_kill",
                                                            "resources.pids_max_events"]}, retry="no")}
    for key in ("attribution", "reason", "cause"):
        if key in completion and completion[key] != metadata[key]:
            return {"attribution": "unknown", "reason": "success_evidence_conflict",
                    "cause": make("unknown", {"conflict": "stored terminal metadata differs from receipt"},
                                  retry="no")}
    return metadata


def _derive(outcome, result, breach, fence, attempts):
    if outcome == "succeeded":
        metadata = success_metadata(result)
        return metadata["cause"] if metadata is not None else make(
            "unknown", {"unreadable": ["exit_code", "resources.oom_kill", "resources.pids_max_events"]},
            fence=fence, attempts=attempts)
    if outcome == "product_failure":
        # The worker's excerpt of command.log is re-cleaned here: the bounds are ours, not the worker's.
        return make("handler_failed", {"exit_code": result.get("exit_code"),
                                       **_excerpt(result)})
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
    evidence.update(_excerpt(result))
    return make("unknown", evidence, fence=fence, attempts=attempts)


def validate_stored(value):
    """Used by the migration test and views: a stored cause has the closed shape."""
    return (isinstance(value, dict) and set(value) == {"kind", "retry", "evidence"}
            and value["kind"] in KINDS and value["retry"] in RETRY and isinstance(value["evidence"], dict))
