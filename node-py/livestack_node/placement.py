"""placement.py — "which of these candidates should serve this request" as a PROBLEM.

A placement is stated as data, answered by a pure function, and recorded:

    problem  = {schema, kind, request, candidates[], context}
    ranking  = {schema, ranker, ranking[{id, score, explain}], attempt_policy}

Today ONE deterministic ranker (`default-v1`, `placement_default.py`, a thin
adapter over `fleet_rank.rank`) answers every kind. This module exists so a
smarter ranker (learned, neural, LLM-based, a world model) can be registered and
selected per request class by CONFIG later, without touching a caller and without
being able to hurt one. None is built here.

The rules this module enforces, not merely documents:

* **Hard constraints are not the ranker's.** Eligibility (ready, serves the kind
  / voice / model, not leased, in policy) is decided BEFORE ranking and stamped on
  each candidate (`eligible`, `ineligible_reason`). A ranker is handed only the
  eligible ones and may only order and score them: an output naming an unknown,
  duplicate or ineligible id is garbage and is replaced by the default.
* **A ranker has a hard time budget and can never take a request down.**
  Exception, timeout, malformed or illegal output -> `default-v1`, and the reason
  is on the decision (`fallback`) and in the log. Never silent (benchday rule 13).
  `default-v1` itself runs inline and is trusted.
* **Forward compatible.** Unknown problem fields, candidate facts and context keys
  are ignored by a ranker that does not use them and preserved in the log. New
  context is namespaced (`net.rtt_ms`, `acct.prefs`). A ranker states the schema
  versions it accepts; a problem of a version it does not accept goes to the
  default.
* **Everything is recorded** to a bounded store (`demand_log.DemandLog`: size x
  files, age window, fail-closed when unset, off the request path): the problem as
  given, ranker name@version, ranking, shadow ranking, chosen, then the observed
  outcome joined by `problem_id`. Shadow mode runs a second ranker on the same
  problem and logs it; its output is never acted on.
* **General across kinds.** `kind` selects which facts apply (`polyasr`, `llm`,
  `embed`, a workload-to-worker placement...). Nothing here knows speech.

Config (env, JSON; none is code):
  HARMONY_PLACEMENT_RANKERS  {"polyasr": "my-ranker@1", "*": "default-v1"}
  HARMONY_PLACEMENT_SHADOW   {"polyasr": "candidate@2"}
  HARMONY_PLACEMENT_BUDGET_S ranker time budget (default 0.25)
  HARMONY_DEMAND_LOG_AGE_DAYS / _DIR / _MAX_MB / _FILES   the log's bounds
"""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

SCHEMA = "placement/v1"
DEFAULT_RANKER = "default-v1"
DEFAULT_BUDGET_S = 0.25
MAX_RECORD_BYTES = 64 * 1024
ATTEMPTS_MIN, ATTEMPTS_MAX = 1, 5
RETRY_AFTER_MIN_S, RETRY_AFTER_MAX_S = 1.0, 300.0

# A ranker: pure function problem -> ranking. `accepts` lists the schemas it
# understands; `name` is "name@version".
Ranker = Callable[[dict], dict]


class Registered:
    def __init__(self, name: str, fn: Ranker, accepts: Iterable[str] = (SCHEMA,), trusted: bool = False):
        self.name, self.fn, self.accepts, self.trusted = name, fn, tuple(accepts), trusted


_REGISTRY: Dict[str, Registered] = {}


def register(name: str, fn: Ranker, accepts: Iterable[str] = (SCHEMA,), trusted: bool = False) -> None:
    """Make `name` ("ranker@version") selectable by config. `trusted` rankers
    run inline (only `default-v1` is); everything else runs under the budget."""
    _REGISTRY[name] = Registered(name, fn, accepts, trusted)


def registered() -> List[str]:
    return sorted(_REGISTRY)


# -- the problem ---------------------------------------------------------------

def make_problem(kind: str, request: Optional[dict], candidates: List[dict],
                 context: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """`candidates[i]` = {id, facts{}, eligible?: bool, ineligible_reason?: str}.
    `eligible` defaults to True: a caller that has no hard constraints states none."""
    return {"schema": SCHEMA, "problem_id": uuid.uuid4().hex, "ts": time.time() if now is None else now,
            "kind": kind, "request": request or {}, "candidates": candidates, "context": context or {}}


def eligible_ids(problem: dict) -> List[str]:
    return [c["id"] for c in problem.get("candidates") or [] if c.get("eligible", True)]


def default_attempt_policy(kind: str) -> dict:
    # Speech is a person waiting: a few cheap round trips, then say why.
    n = 3 if kind in ("polyasr", "polytts", "asr", "tts") else 2
    return {"max_attempts": n, "per_attempt_timeout_s": None,
            "retry_after": {"min_s": RETRY_AFTER_MIN_S, "max_s": RETRY_AFTER_MAX_S}}


def clamp_attempt_policy(policy: Any, kind: str) -> dict:
    """A ranker may propose a policy; the bounds are ours."""
    base = default_attempt_policy(kind)
    if not isinstance(policy, dict):
        return base
    out = dict(base)
    n = policy.get("max_attempts")
    if isinstance(n, int) and not isinstance(n, bool):
        out["max_attempts"] = max(ATTEMPTS_MIN, min(ATTEMPTS_MAX, n))
    t = policy.get("per_attempt_timeout_s")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
        out["per_attempt_timeout_s"] = float(t)
    return out


# -- validating a ranker's answer ---------------------------------------------

def validate_ranking(problem: dict, out: Any) -> Optional[str]:
    """None when `out` is a legal ranking of `problem`, else why not."""
    if not isinstance(out, dict) or not isinstance(out.get("ranking"), list):
        return "output is not {ranking: [...]}"
    allowed = set(eligible_ids(problem))
    seen = set()
    for i, row in enumerate(out["ranking"]):
        cid = row.get("id") if isinstance(row, dict) else None
        if cid not in allowed:
            known = {c.get("id") for c in problem.get("candidates") or []}
            return (f"ranking[{i}] names {cid!r}, which is "
                    + ("ineligible" if cid in known else "not a candidate"))
        if cid in seen:
            return f"ranking[{i}] repeats {cid!r}"
        seen.add(cid)
        sc = row.get("score")
        if sc is not None and (isinstance(sc, bool) or not isinstance(sc, (int, float)) or sc != sc):
            return f"ranking[{i}].score is not a number"
    return None


# -- running one ranker under the budget --------------------------------------

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="placement-ranker")


def _call(entry: Registered, problem: dict, budget_s: float):
    """(output, None) or (None, why). Never raises."""
    if problem.get("schema") not in entry.accepts:
        return None, f"schema {problem.get('schema')!r} not accepted (accepts {list(entry.accepts)})"
    try:
        if entry.trusted:
            out = entry.fn(problem)
        else:
            # A thread cannot be killed: a ranker that overruns is abandoned and
            # its result discarded. Rankers are pure, so abandoning one is safe.
            out = _POOL.submit(entry.fn, copy.deepcopy(problem)).result(timeout=budget_s)
    except concurrent.futures.TimeoutError:
        return None, f"timeout after {budget_s:.3f}s"
    except Exception as e:  # noqa: BLE001 - a ranker's failure is data
        return None, f"{type(e).__name__}: {str(e)[:160]}"
    bad = validate_ranking(problem, out)
    return (None, bad) if bad else (out, None)


def _config(var: str) -> dict:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def select(kind: str, var: str = "HARMONY_PLACEMENT_RANKERS") -> Optional[str]:
    cfg = _config(var)
    return cfg.get(kind) or cfg.get("*")


_LOG = None


def decision_log():
    global _LOG
    if _LOG is None:
        from .demand_log import demand_log_from_env
        _LOG = demand_log_from_env("placement", log=lambda m: print(m, flush=True))
    return _LOG


def set_decision_log(log) -> None:
    global _LOG
    _LOG = log


def _record(**fields) -> None:
    try:
        row = {"ts": time.time(), **fields}
        if len(json.dumps(row, default=str)) > MAX_RECORD_BYTES:
            # Bound each record too: keep the decision, drop the bulk, say so.
            row["problem"] = {k: v for k, v in (row.get("problem") or {}).items()
                              if k in ("schema", "problem_id", "kind", "request")}
            row["problem_truncated"] = True
        decision_log().record(**row)
    except Exception:  # noqa: BLE001 - logging must never take the request with it
        pass


# -- the one entry point -------------------------------------------------------

def run(problem: dict, *, budget_s: Optional[float] = None) -> dict:
    """Decide. Returns {ranker, ranking[], chosen, attempt_policy, fallback, shadow,
    problem_id}. Always returns a decision for a legal problem: the default
    answers whenever the selected ranker cannot."""
    kind = problem.get("kind", "")
    budget = budget_s if budget_s is not None else float(
        os.environ.get("HARMONY_PLACEMENT_BUDGET_S", DEFAULT_BUDGET_S))
    wanted = select(kind) or DEFAULT_RANKER
    default = _REGISTRY[DEFAULT_RANKER]
    fallback = None
    out, ranker_name = None, wanted
    if wanted != DEFAULT_RANKER:
        entry = _REGISTRY.get(wanted)
        if entry is None:
            fallback = {"from": wanted, "why": "not registered"}
        else:
            out, why = _call(entry, problem, budget)
            if why:
                fallback = {"from": wanted, "why": why}
    if out is None:
        out, why = _call(default, problem, budget)
        ranker_name = DEFAULT_RANKER
        if why:  # the default itself failed: a bug, and visible as one
            out = {"ranking": [{"id": i, "score": None, "explain": {"fallback": "input order"}}
                               for i in eligible_ids(problem)]}
            fallback = {"from": DEFAULT_RANKER, "why": why}
    ranking = [{"id": r["id"], "score": r.get("score"), "explain": r.get("explain") or {}}
               for r in out["ranking"]]
    shadow = None
    sname = select(kind, "HARMONY_PLACEMENT_SHADOW")
    if sname and sname != ranker_name:
        sentry = _REGISTRY.get(sname)
        if sentry is None:
            shadow = {"ranker": sname, "error": "not registered"}
        else:
            sout, swhy = _call(sentry, problem, budget)
            shadow = ({"ranker": sname, "error": swhy} if swhy else
                      {"ranker": sname, "ranking": [{"id": r["id"], "score": r.get("score"),
                                                     "explain": r.get("explain") or {}} for r in sout["ranking"]]})
    decision = {"problem_id": problem.get("problem_id"), "ranker": ranker_name, "ranking": ranking,
                "chosen": ranking[0]["id"] if ranking else None,
                "attempt_policy": clamp_attempt_policy(out.get("attempt_policy"), kind),
                "fallback": fallback, "shadow": shadow}
    _record(event="decision", problem=problem, **{k: v for k, v in decision.items() if k != "problem_id"},
            problem_id=problem.get("problem_id"))
    return decision


def record_outcome(problem_id: Optional[str], *, target: Optional[str], success: bool,
                   cause: Optional[str] = None, first_byte_ms: Optional[float] = None,
                   first_partial_ms: Optional[float] = None, final_ms: Optional[float] = None,
                   attempts: Optional[list] = None, **extra) -> None:
    """What actually happened, joined to the decision by `problem_id`. Unknown
    latencies are None, never 0."""
    if not problem_id:
        return
    _record(event="outcome", problem_id=problem_id, target=target, success=success, cause=cause,
            first_byte_ms=first_byte_ms, first_partial_ms=first_partial_ms, final_ms=final_ms,
            attempts=attempts, **extra)


# -- offline replay (a trivial harness; the interface is the point) -----------

def replay(records: Iterable[dict], ranker: Ranker, name: str = "candidate") -> dict:
    """Feed logged problems to `ranker`, compare with what actually performed.

    `records` are decision and outcome rows (`read_demand` of the placement log).
    For each problem with a successful outcome: did `ranker` put the target that
    actually succeeded first, and at what rank? Illegal rankings count as
    `illegal`, never as a miss. Evaluation proper (counterfactual estimates,
    off-policy correction) is deliberately not here."""
    decisions: Dict[str, dict] = {}
    ok: Dict[str, str] = {}
    for r in records:
        pid = r.get("problem_id")
        if r.get("event") == "decision" and isinstance(r.get("problem"), dict):
            decisions[pid] = r
        elif r.get("event") == "outcome" and r.get("success") and r.get("target"):
            ok[pid] = r["target"]
    report = {"ranker": name, "problems": 0, "scored": 0, "top1": 0, "illegal": 0, "mean_rank": None}
    ranks = []
    for pid, d in decisions.items():
        report["problems"] += 1
        if pid not in ok:
            continue
        try:
            out = ranker(copy.deepcopy(d["problem"]))
        except Exception:  # noqa: BLE001
            report["illegal"] += 1
            continue
        if validate_ranking(d["problem"], out):
            report["illegal"] += 1
            continue
        order = [row["id"] for row in out["ranking"]]
        if ok[pid] in order:
            rank = order.index(ok[pid]) + 1
            ranks.append(rank)
            report["scored"] += 1
            report["top1"] += rank == 1
    if ranks:
        report["mean_rank"] = round(sum(ranks) / len(ranks), 3)
    return report
