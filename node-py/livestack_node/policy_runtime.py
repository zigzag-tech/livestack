"""policy_runtime.py — the fleet scheduler's target choice as a compiled policy.

``fleet_scheduler.schedule()`` places jobs; the per-job choice of WHERE is the
policy ``livestack.fleet.choose_target`` (family v1). Design:
``openspec/changes/scheduler-policy-routine/design.md`` §2–§3, and the Jingway
framework it consumes, ``jingway/openspec/changes/compiled-policy-routines/``.

This module holds the pure-Python REFERENCE of that family. The reference is a
thin wrapper over ``fleet_scheduler._feasible_candidates`` and ``_score`` — it
does not re-implement them — so it cannot drift from the code it replaced, and
the native Rust family is tested differentially against it.

Pure stdlib, like the rest of ``livestack_node``: the native module is optional.
"""
from __future__ import annotations

import json as _json
import math as _math
import os as _os
import threading as _threading
import time as _time
from types import SimpleNamespace
from typing import (
    Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple,
)

from .fleet_scheduler import (
    DEFAULT_DISTANCE_BY_SLA, DEFAULT_SLA_SLACK_S, CostModel, Job, SchedulerPolicy,
    Sla, Target, Tier, Weights, _Fleet, _distance_n, _feasible_candidates, _fits,
    _norm, _score, _selector_matches, _utilization_n,
)

POLICY_ID = "livestack.fleet.choose_target"
FAMILY = (POLICY_ID, 1)

#: §2.3, exactly today's code. Flat names; dotted for grouping.
DEFAULT_PARAMS: Dict[str, float] = {
    "w_resource": 1.0,
    "w_budget": 1.0,
    "w_speed": 1.0,
    "w_distance": 2.0,
    "w_utilization": 1.0,
    "distance_by_sla.interactive": 1.0,
    "distance_by_sla.normal": 0.5,
    "distance_by_sla.batch": 0.1,
    "local_bonus": 1.0,
    "locality_bonus": 0.5,
    "sla_slack_s.interactive": 30.0,
    "sla_slack_s.normal": 1800.0,
    "sla_slack_s.batch": 43200.0,
}

_SLA_NAMES = {Sla.INTERACTIVE: "interactive", Sla.NORMAL: "normal", Sla.BATCH: "batch"}
_SLA_BY_NAME = {v: k for k, v in _SLA_NAMES.items()}

#: The version string decisions carry when the params came from a
#: ``SchedulerPolicy`` rather than from an artifact (``runtime=None``). Python
#: never hashes an artifact (J§3.2); this is a marker, not a version.
SCHEDULER_POLICY_VERSION = "scheduler_policy"


def params_from_policy(policy: SchedulerPolicy) -> Dict[str, float]:
    """The family params a ``SchedulerPolicy`` implies, with the same fallbacks
    ``effective_deadline`` and ``schedule()`` apply to a mapping that lacks an SLA
    (the NORMAL value)."""
    w = policy.weights
    out = {
        "w_resource": w.resource, "w_budget": w.budget, "w_speed": w.speed,
        "w_distance": w.distance, "w_utilization": w.utilization,
        "local_bonus": policy.local_bonus, "locality_bonus": policy.locality_bonus,
    }
    for sla, name in _SLA_NAMES.items():
        out[f"distance_by_sla.{name}"] = policy.distance_by_sla.get(
            sla, DEFAULT_DISTANCE_BY_SLA[Sla.NORMAL])
        out[f"sla_slack_s.{name}"] = policy.sla_slack_s.get(
            sla, DEFAULT_SLA_SLACK_S[Sla.NORMAL])
    return out


def _policy_from_params(params: Mapping[str, float]) -> SchedulerPolicy:
    return SchedulerPolicy(
        weights=Weights(resource=params["w_resource"], budget=params["w_budget"],
                        speed=params["w_speed"], distance=params["w_distance"],
                        utilization=params["w_utilization"]),
        sla_slack_s={s: params[f"sla_slack_s.{n}"] for s, n in _SLA_NAMES.items()},
        distance_by_sla={s: params[f"distance_by_sla.{n}"] for s, n in _SLA_NAMES.items()},
        local_bonus=params["local_bonus"], locality_bonus=params["locality_bonus"])


# --- §2.1 / §2.2: what one decision sees ------------------------------------
def build_ctx_and_candidates(job: Job, fleet_W: _Fleet, targets, policy: SchedulerPolicy
                             ) -> Tuple[dict, List[dict]]:
    """The family context and one candidate per target, in ``targets`` order,
    as the JSON shapes of design §2.1/§2.2.

    Everything that depends on state mutated across jobs in one ``schedule()``
    call arrives as a boolean evaluated NOW, at this job's turn: free room
    (``fits_now``), pool headroom (``headroom_ok``), instance size
    (``fits_instance``). A boolean that does not apply is ``False`` and is never
    read. ``policy`` is accepted for the signature the design names; no v1
    feature depends on it (the slacks and weights are params)."""
    ctx = {"now": fleet_W.now,
           "job": {"id": job.id, "sla": _SLA_NAMES[job.sla],
                   "created_at": job.created_at, "deadline": job.deadline,
                   "est_duration_s": job.est_duration_s,
                   "locality_host": job.locality_host}}
    cands = []
    for t in targets:
        pool = (not t.running) and t.elastic
        cands.append({"id": t.id, "features": {
            "host_id": t.host_id, "tier": t.tier.name,
            "running": t.running, "elastic": t.elastic,
            "selector_match": _selector_matches(t, job.selector),
            "fits_now": t.running and _fits(job.need, fleet_W.free.get(t.id, {})),
            "headroom_ok": pool and fleet_W.headroom(t) > 0,
            "fits_instance": pool and _fits(job.need, t.capacity),
            "provision_latency_s": t.provision_latency_s,
            "cost_per_hour": t.cost.per_hour, "cost_per_job": t.cost.per_job,
            "distance_ms": t.distance_ms, "utilization": t.utilization,
        }})
    return ctx, cands


# --- §2.4: the reference ------------------------------------------------------
# One synthetic resource and one synthetic label. The features already carry the
# answers to "does it fit" and "does the selector match"; these let
# `_feasible_candidates` ask its own questions and get exactly those answers.
_UNIT = {"_feature": 1.0}
_SEL_KEY, _SEL_VAL = "_selector_match", "1"


class _FeatureFleet(_Fleet):
    """A `_Fleet` whose free room and pool headroom are the candidates'
    booleans, so `_feasible_candidates` runs unchanged over features."""

    def __init__(self, now: float, targets, headroom_ok: Mapping[str, bool],
                 free: Mapping[str, dict]):
        # `targets` is the one attribute _feasible_candidates reads from state.
        self.state = SimpleNamespace(targets=targets)
        self.now = now
        self.free = dict(free)
        self.provisioned = {}
        self.admitted_to = set()
        self._headroom_ok = headroom_ok

    def headroom(self, t: Target) -> int:
        return 1 if self._headroom_ok.get(t.id) else 0


def _fmt(x: float) -> str:
    return f"{x:.6f}"


def choose_target_reference(params: Mapping[str, float], ctx: dict,
                            candidates: List[dict]) -> List[dict]:
    """Pure Python mirror of §2.4. Returns one row per candidate, in input order:
    ``{id, eligible, score, explorable, reason}``."""
    pol = _policy_from_params(params)
    j = ctx["job"]
    job = Job(id=j["id"], kind="", need=dict(_UNIT),
              created_at=j["created_at"], sla=_SLA_BY_NAME[j["sla"]],
              deadline=j.get("deadline"), est_duration_s=j["est_duration_s"],
              selector={_SEL_KEY: _SEL_VAL}, locality_host=j.get("locality_host"))
    targets, headroom_ok, free = [], {}, {}
    for c in candidates:
        f = c["features"]
        running = bool(f["running"])
        targets.append(Target(
            id=c["id"], host_id=f["host_id"], tier=Tier[f["tier"]],
            capacity=dict(_UNIT) if f["fits_instance"] else {},
            cost=CostModel(per_hour=f["cost_per_hour"], per_job=f["cost_per_job"]),
            provision_latency_s=f["provision_latency_s"], running=running,
            elastic=bool(f["elastic"]),
            labels={_SEL_KEY: _SEL_VAL} if f["selector_match"] else {},
            distance_ms=f["distance_ms"], utilization=f["utilization"]))
        headroom_ok[c["id"]] = bool(f["headroom_ok"])
        if running:
            free[c["id"]] = dict(_UNIT) if f["fits_now"] else {}
    fleet = _FeatureFleet(ctx["now"], tuple(targets), headroom_ok, free)
    rejected: List[Tuple[str, str]] = []
    eligible = _feasible_candidates(fleet, job, pol, rejected=rejected)
    why = dict(rejected)
    d_scale = pol.distance_by_sla[job.sla]
    w = pol.weights
    by_id = {}
    for cand in eligible:
        score = _score(cand, eligible, w, d_scale)
        cost_n = _norm(cand.est_cost, [e.est_cost for e in eligible])
        eta_n = _norm(cand.eta, [e.eta for e in eligible])
        reason = (f"scored:{_fmt(score)} cost_n={_fmt(cost_n)} eta_n={_fmt(eta_n)} "
                  f"dist_n={_fmt(_distance_n(cand, eligible))} "
                  f"util_n={_fmt(_utilization_n(cand, eligible))} "
                  f"local={_fmt(cand.local_bonus)}")
        by_id[cand.target.id] = (score, reason, cand.target)
    rows = []
    for t in targets:
        hit = by_id.get(t.id)
        if hit is None:
            rows.append({"id": t.id, "eligible": False, "score": None,
                         "explorable": False, "reason": why[t.id]})
        else:
            score, reason, _ = hit
            rows.append({"id": t.id, "eligible": True, "score": score,
                         "explorable": t.running and t.tier != Tier.LAST_RESORT,
                         "reason": reason})
    return rows


def greedy_decision(rows: List[dict], *, decision_id: str, artifact_version: str,
                    ctx: dict, candidates: List[dict]) -> dict:
    """The Jingway `Decision` (J§4.3) for a greedy policy: lowest score, ties to
    the EARLIEST input position (Python's `min` over a list), no exploration."""
    greedy = None
    best = None
    for r in rows:
        if r["eligible"] and (best is None or r["score"] < best):
            greedy, best = r["id"], r["score"]
    return {
        "decision_id": decision_id, "policy_id": POLICY_ID,
        "artifact_version": artifact_version,
        "family": {"id": FAMILY[0], "version": FAMILY[1]},
        "context": ctx, "candidates": candidates,
        "rows": rows, "greedy": greedy, "chosen": greedy, "explored": False,
        "explore_set": [greedy] if greedy is not None else [],
        "propensities": {greedy: 1.0} if greedy is not None else {},
        "exploration": {"enabled": False, "epsilon": 0.0, "margin": 0.0, "draw": 0.0},
        "escalate": None, "shadow": [],
    }


class _ReferenceRuntime:
    """What ``schedule(runtime=None)`` decides with: the reference, the
    ``SchedulerPolicy``'s own params, no exploration, nothing recorded."""

    def __init__(self, params: Mapping[str, float]):
        self.params = dict(params)

    def decide(self, ctx: dict, candidates: List[dict], decision_id: str) -> dict:
        rows = choose_target_reference(self.params, ctx, candidates)
        return greedy_decision(rows, decision_id=decision_id,
                               artifact_version=SCHEDULER_POLICY_VERSION,
                               ctx=ctx, candidates=candidates)


_DEFAULT_POLICY = SchedulerPolicy()
_REFERENCE_RUNTIME = _ReferenceRuntime(params_from_policy(_DEFAULT_POLICY))


def reference_runtime(policy: Optional[SchedulerPolicy]) -> _ReferenceRuntime:
    """The private reference runtime for ``policy`` (shared when it is the
    default one)."""
    if policy is None or policy == _DEFAULT_POLICY:
        return _REFERENCE_RUNTIME
    return _ReferenceRuntime(params_from_policy(policy))


# --- the broker's runtime (design §3, §6 loading, §7 modes) -------------------

#: design §2.3 hard bounds. Used ONLY to sanity-check a file when the native
#: module is absent (it validates everything when present, and is the only
#: thing that may compute a version).
PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "w_resource": (0, 10), "w_budget": (0, 10), "w_speed": (0, 10),
    "w_distance": (0, 10), "w_utilization": (0, 10),
    "distance_by_sla.interactive": (0, 5), "distance_by_sla.normal": (0, 5),
    "distance_by_sla.batch": (0, 5),
    "local_bonus": (0, 5), "locality_bonus": (0, 5),
    "sla_slack_s.interactive": (1, 604800), "sla_slack_s.normal": (1, 604800),
    "sla_slack_s.batch": (1, 604800),
}
MAX_EPSILON = 0.10
ARTIFACT_SCHEMA = "jingway.policy_artifact/v1"
#: The version decisions carry when no artifact file is loaded and the native
#: module (the only thing that hashes) is absent.
DEFAULTS_VERSION = "defaults"
RELOAD_INTERVAL_S = 5.0
MAX_MISMATCH_FILES = 100
MAX_SHADOWS = 2
SCORE_TOLERANCE = 1e-12
MODES = ("0", "auto", "compare")


def default_policy_dir() -> str:
    return _os.path.expanduser("~/.local/share/livestack/policy")


def defaults_artifact() -> dict:
    """Today's defaults as an artifact, exploration off (task 6.3's first
    artifact). ``version`` is left for the native module to compute."""
    return {"schema": ARTIFACT_SCHEMA, "policy_id": POLICY_ID,
            "family": {"id": FAMILY[0], "version": FAMILY[1]},
            "version": "", "parent_version": None,
            "params": dict(DEFAULT_PARAMS),
            "exploration": {"enabled": False, "epsilon": 0.0, "margin": 0.0},
            "provenance": {"created_by": "code:defaults",
                           "notes": "fleet_scheduler.py defaults"}}


class NativePolicy:
    """The optional native module (``livestack_policy``, built from
    ``native/policy``), behind the three calls this runtime needs. Tests inject
    a fake with the same surface."""

    def __init__(self, module):
        self.m = module

    def load(self, text: str) -> Tuple[Any, Optional[str], List[dict]]:
        """``(handle, version, violations)``; a refused artifact has no handle."""
        try:
            art = self.m.load_artifact(text)
        except ValueError as e:  # PolicyArtifactError subclasses ValueError
            v = getattr(e, "violations", None)
            return None, None, (list(v) if v else
                                [{"code": "not_an_artifact", "detail": str(e)}])
        return art, art.version, []

    def decide(self, handle, ctx: dict, candidates: List[dict], decision_id: str) -> dict:
        return self.m.decide(handle, POLICY_ID, ctx, candidates, decision_id)

    def version_of(self, art: dict) -> Optional[str]:
        """The artifact version, computed by the crate (J§3.2: Python never
        hashes). None when this build of the module has no ``artifact_version``
        — the jingway-policy-py surface of 2026-09-24 exposes only
        ``load_artifact``, which refuses an artifact whose version field is not
        already correct, so the compiled defaults cannot be loaded natively
        without it."""
        fn = getattr(self.m, "artifact_version", None)
        return fn(_json.dumps(art)) if fn is not None else None

    def recorder(self, **cfg):
        return self.m.Recorder(**cfg)


def import_native() -> Optional[NativePolicy]:
    try:
        from livestack_policy import policy as mod  # type: ignore
    except ImportError:
        return None
    return NativePolicy(mod)


def python_violations(art: Any) -> List[dict]:
    """A structural check for when no native validator exists. It never
    computes a version (J§3.2: only the crate hashes)."""
    out: List[dict] = []

    def v(code, detail):
        out.append({"code": code, "detail": detail})
    if not isinstance(art, dict):
        return [{"code": "not_an_artifact", "detail": "not a JSON object"}]
    if art.get("schema") != ARTIFACT_SCHEMA:
        v("schema_mismatch", f"schema {art.get('schema')!r}")
    fam = art.get("family") or {}
    if fam.get("id") != FAMILY[0] or fam.get("version") != FAMILY[1]:
        v("family_mismatch", f"family {fam!r}")
    params = art.get("params")
    if not isinstance(params, dict):
        v("param_missing", "no params object")
        params = {}
    for name, (lo, hi) in PARAM_BOUNDS.items():
        if name not in params:
            v("param_missing", name)
            continue
        x = params[name]
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            v("param_kind", f"{name}={x!r}")
        elif not _math.isfinite(x):
            v("param_not_finite", name)
        elif not lo <= x <= hi:
            v("param_out_of_bounds", f"{name}={x} outside [{lo}, {hi}]")
    for name in params:
        if name not in PARAM_BOUNDS:
            v("param_unknown", name)
    e = art.get("exploration") or {}
    eps, margin = e.get("epsilon", 0.0), e.get("margin", 0.0)
    if (not isinstance(eps, (int, float)) or not isinstance(margin, (int, float))
            or not 0 <= eps <= MAX_EPSILON or margin < 0
            or (e.get("enabled") and eps == 0)):
        v("exploration_invalid", f"exploration {e!r}")
    return out


class _Loaded:
    """One validated artifact as this process holds it."""

    def __init__(self, art: dict, version: str, handle, verified: bool, loaded_at: float):
        self.art = art
        self.version = version
        self.handle = handle
        self.verified = verified
        self.loaded_at = loaded_at
        self.params = {k: float(x) for k, x in art["params"].items()}
        ex = art.get("exploration") or {}
        self.exploring = bool(ex.get("enabled"))

    def summary(self) -> dict:
        return {"version": self.version, "provenance": self.art.get("provenance"),
                "loaded_at": self.loaded_at, "version_verified": self.verified}


def _code(reason: str) -> str:
    return reason.split(" ", 1)[0]


def _rows_agree(a: List[dict], b: List[dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if (x["id"] != y["id"] or x["eligible"] != y["eligible"]
                or _code(x["reason"]) != _code(y["reason"])):
            return False
        if x["eligible"] and abs(x["score"] - y["score"]) > SCORE_TOLERANCE:
            return False
    return True


class PolicyRuntime:
    """One per broker process: which artifact is active, which implementation
    decides, what is shadowed, and the policy record stream.

    ``mode`` is ``LIVESTACK_POLICY_NATIVE``: ``0`` reference only; ``auto``
    native when importable, else the reference and degraded; ``compare`` both,
    acting on the REFERENCE and counting every disagreement.

    ``native`` defaults to importing ``livestack_policy``; pass ``None`` for
    none or a fake. ``recorder`` defaults to the native ``Recorder`` when the
    module is present (and the mode is not ``0``); it is injectable with the
    same surface: ``record(dict) -> bool`` (False = dropped), ``stats() ->
    dict``, ``close()``. ``validator`` (``text -> (version, violations)``)
    gates the PUT route (task 3.5) and defaults to the native loader.
    """

    _AUTO = object()

    def __init__(self, policy_dir: str, mode: str = "auto",
                 self_principals: Iterable[str] = (),
                 log: Callable[[str], None] = lambda *_: None, *,
                 native=_AUTO, recorder=_AUTO, validator=_AUTO,
                 clock: Callable[[], float] = _time.monotonic,
                 wall: Callable[[], float] = _time.time,
                 records_max_mb: float = 128.0, records_files: int = 16,
                 records_age_days: Optional[int] = None):
        if mode not in MODES:
            raise ValueError(f"LIVESTACK_POLICY_NATIVE={mode!r}; expected one of {MODES}")
        self.policy_dir = policy_dir
        self.mode = mode
        self.self_principals: FrozenSet[str] = frozenset(self_principals)
        self._log = log
        self._clock = clock
        self._wall = wall
        self.native = import_native() if native is PolicyRuntime._AUTO else native
        if validator is PolicyRuntime._AUTO:
            validator = ((lambda text: self.native.load(text)[1:])
                         if self.native is not None else None)
        self.validator = validator
        self.records_dir = _os.path.join(policy_dir, "records")
        self.records_unavailable_reason: Optional[str] = None
        if recorder is PolicyRuntime._AUTO:
            recorder = None
            if self.native is None:
                self.records_unavailable_reason = "native_unavailable"
            elif mode == "0":
                self.records_unavailable_reason = "mode_0"
            else:
                try:
                    recorder = self.native.recorder(
                        dir=self.records_dir, stem=POLICY_ID,
                        max_file_bytes=int(records_max_mb * 1024 * 1024),
                        max_files=int(records_files), max_age_days=records_age_days)
                except Exception as e:  # noqa: BLE001 — reported, never fatal
                    self.records_unavailable_reason = f"recorder_open_failed: {e}"
                    log(f"[policy] record stream unavailable: {e}; decisions "
                        f"are made but NOT recorded, and /fleet reports it")
        self.recorder = recorder
        self._lock = _threading.Lock()
        self._active: Optional[_Loaded] = None
        self._defaults: Optional[_Loaded] = None
        self._previous_version: Optional[str] = None
        self._shadows: List[_Loaded] = []
        self._mtimes: Dict[str, Optional[int]] = {}
        self._last_poll: Optional[float] = None
        self.last_load_error: Optional[str] = None
        self.mismatches = 0
        self._mismatch_seq = 0
        self.skipped_no_choice = 0
        self.records_dropped_here = 0
        self.reload_if_changed(force=True)

    # -- files -----------------------------------------------------------------
    def _path(self, role: str) -> str:
        return _os.path.join(self.policy_dir, f"{POLICY_ID}.{role}.json")

    def _load_text(self, text: str) -> Tuple[Optional[_Loaded], List[dict]]:
        try:
            art = _json.loads(text)
        except ValueError as e:
            return None, [{"code": "not_an_artifact", "detail": f"invalid JSON: {e}"}]
        now = self._wall()
        if self.native is not None:
            handle, version, violations = self.native.load(text)
            if violations:
                return None, violations
            return _Loaded(art, version, handle, True, now), []
        violations = python_violations(art)
        if violations:
            return None, violations
        return _Loaded(art, str(art.get("version") or ""), None, False, now), []

    def _defaults_loaded(self) -> _Loaded:
        if self._defaults is None:
            art = defaults_artifact()
            if self.native is not None:
                art["version"] = self.native.version_of(art) or ""
                handle, version, violations = self.native.load(_json.dumps(art))
                if not violations:
                    self._defaults = _Loaded(art, version, handle, True, self._wall())
                    return self._defaults
                # The reference decides on defaults instead, and /fleet says so.
                self._log(f"[policy] the compiled defaults cannot be loaded "
                          f"natively ({violations}); deciding on defaults with "
                          f"the reference")
                art["version"] = ""
            self._defaults = _Loaded(art, DEFAULTS_VERSION, None, False, self._wall())
        return self._defaults

    def _mtime(self, path: str) -> Optional[int]:
        try:
            return _os.stat(path).st_mtime_ns
        except OSError:
            return None

    def reload_if_changed(self, force: bool = False) -> None:
        """Poll the artifact files' mtimes, at most every ``RELOAD_INTERVAL_S``,
        and re-read only a file whose mtime changed. A file that fails
        validation leaves the previously loaded artifact in force."""
        now = self._clock()
        with self._lock:
            if not force and self._last_poll is not None \
                    and now - self._last_poll < RELOAD_INTERVAL_S:
                return
            self._last_poll = now
            for role in ("active", "previous", "shadow"):
                path = self._path(role)
                m = self._mtime(path)
                if not force and m == self._mtimes.get(role):
                    continue
                self._mtimes[role] = m
                self._reload_role(role, path, m)

    def _reload_role(self, role: str, path: str, mtime: Optional[int]) -> None:
        if mtime is None:
            if role == "active":
                self._active = None
            elif role == "shadow":
                self._shadows = []
            else:
                self._previous_version = None
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            self._load_failed(role, [{"code": "unreadable", "detail": str(e)}])
            return
        if role == "shadow":
            try:
                arr = _json.loads(text)
            except ValueError as e:
                self._load_failed(role, [{"code": "not_an_artifact", "detail": str(e)}])
                return
            if not isinstance(arr, list) or len(arr) > MAX_SHADOWS:
                self._load_failed(role, [{"code": "shadow_invalid",
                                          "detail": f"expected a list of <= {MAX_SHADOWS}"}])
                return
            loaded = []
            for a in arr:
                one, violations = self._load_text(_json.dumps(a))
                if violations:
                    self._load_failed(role, violations)
                    return
                loaded.append(one)
            self._shadows = loaded
            return
        loaded, violations = self._load_text(text)
        if violations:
            self._load_failed(role, violations)
            return
        if role == "active":
            self._active = loaded
            self.last_load_error = None
            self._log(f"[policy] loaded {POLICY_ID} {loaded.version} "
                      f"(verified={loaded.verified})")
        else:
            self._previous_version = loaded.version

    def _load_failed(self, role: str, violations: List[dict]) -> None:
        detail = "; ".join(f"{v.get('code')}: {v.get('detail', '')}" for v in violations)
        self.last_load_error = f"{role}: {detail}"
        # One line naming every violation; the previous artifact stays in force.
        self._log(f"[policy] {self._path(role)} refused, keeping the previous "
                  f"artifact: {detail}")

    # -- deciding ----------------------------------------------------------------
    @property
    def active(self) -> _Loaded:
        return self._active or self._defaults_loaded()

    @property
    def artifact_version(self) -> str:
        return self.active.version

    def decide(self, ctx: dict, candidates: List[dict], decision_id: str, *,
               explore: bool = True) -> dict:
        """The Jingway `Decision` (as a dict, with ``context``/``candidates``
        and ``shadow`` attached). ``explore=False`` forces the greedy choice
        (the /fleet/plan path, design §5)."""
        self.reload_if_changed()
        art = self.active
        ref_rows = None
        if self.mode == "auto" and art.handle is not None:
            d = dict(self.native.decide(art.handle, ctx, candidates, decision_id))
            if not explore:
                d = self._greedy_view(d)
        else:
            ref_rows = choose_target_reference(art.params, ctx, candidates)
            d = greedy_decision(ref_rows, decision_id=decision_id,
                                artifact_version=art.version, ctx=ctx,
                                candidates=candidates)
            if self.mode == "compare" and art.handle is not None:
                nat = self.native.decide(art.handle, ctx, candidates, decision_id)
                if not (_rows_agree(ref_rows, nat["rows"]) and nat["greedy"] == d["greedy"]):
                    self._mismatch(decision_id, ctx, candidates, ref_rows, d["greedy"], nat)
        d["context"], d["candidates"] = ctx, candidates
        d["shadow"] = [{"artifact_version": s.version,
                        "chosen": self._shadow_choice(s, ctx, candidates, decision_id)}
                       for s in self._shadows]
        return d

    @staticmethod
    def _greedy_view(d: dict) -> dict:
        g = d.get("greedy")
        ex = dict(d.get("exploration") or {})
        ex["enabled"] = False
        return {**d, "chosen": g, "explored": False,
                "explore_set": [g] if g is not None else [],
                "propensities": {g: 1.0} if g is not None else {},
                "exploration": ex}

    def _shadow_choice(self, s: _Loaded, ctx, candidates, decision_id) -> Optional[str]:
        """Greedy only (J§10.2), and never acted on."""
        if self.mode == "auto" and s.handle is not None:
            return self.native.decide(s.handle, ctx, candidates, decision_id)["greedy"]
        rows = choose_target_reference(s.params, ctx, candidates)
        return greedy_decision(rows, decision_id=decision_id, artifact_version=s.version,
                               ctx=ctx, candidates=candidates)["greedy"]

    def without_exploration(self) -> "_GreedyView":
        """This runtime with exploration forced off, for ``schedule()`` on the
        plan path, where the same queued job is re-planned every tick."""
        return _GreedyView(self)

    def _mismatch(self, decision_id, ctx, candidates, ref_rows, ref_greedy, nat) -> None:
        with self._lock:
            self.mismatches += 1
            self._mismatch_seq += 1
            seq = self._mismatch_seq
        self._log(f"[policy] compare mismatch on decision {decision_id or '-'}: "
                  f"reference greedy={ref_greedy} native greedy={nat.get('greedy')}")
        d = _os.path.join(self.policy_dir, "mismatches")
        try:
            _os.makedirs(d, exist_ok=True)
            name = f"{int(self._wall() * 1000):013d}-{seq:06d}-{decision_id or 'none'}.json"
            with open(_os.path.join(d, name), "w", encoding="utf-8") as fh:
                _json.dump({"decision_id": decision_id, "artifact_version": self.artifact_version,
                            "context": ctx, "candidates": candidates,
                            "reference": {"rows": ref_rows, "greedy": ref_greedy},
                            "native": {"rows": nat.get("rows"), "greedy": nat.get("greedy")}},
                           fh, sort_keys=True)
            files = sorted(f for f in _os.listdir(d) if f.endswith(".json"))
            for old in files[:max(0, len(files) - MAX_MISMATCH_FILES)]:
                _os.remove(_os.path.join(d, old))
        except OSError as e:
            self._log(f"[policy] could not write mismatch case: {e}")

    # -- recording ---------------------------------------------------------------
    def record_decision(self, decision: Optional[dict], *, principal: Optional[str],
                        ts: Optional[float] = None) -> bool:
        """Write one committed decision to the policy stream (J§6.2). A decision
        that chose nothing — or none at all, for a quota refusal that never
        reached the choice — is NOT written (design §1); it is counted in
        ``skipped_no_choice``. Returns True iff the record was queued."""
        if decision is None or decision.get("chosen") is None:
            with self._lock:
                self.skipped_no_choice += 1
            return False
        if self.recorder is None:
            return False
        rec = {"record": "policy_decision",
               "decision_id": decision["decision_id"],
               "ts": self._wall() if ts is None else ts,
               "policy_id": decision.get("policy_id", POLICY_ID),
               "family": decision.get("family") or {"id": FAMILY[0], "version": FAMILY[1]},
               "artifact_version": decision["artifact_version"],
               "context": decision["context"], "candidates": decision["candidates"],
               "rows": decision["rows"], "greedy": decision["greedy"],
               "chosen": decision["chosen"], "explored": decision["explored"],
               "explore_set": decision["explore_set"],
               "propensities": decision["propensities"],
               "exploration": decision["exploration"],
               "escalate": decision.get("escalate"),
               "principal": principal or "anonymous",
               "self_traffic": bool(principal) and principal in self.self_principals,
               "shadow": decision.get("shadow") or []}
        return self._record(rec)

    def record_outcome(self, decision_id: str, outcome_id: str, value: float,
                       *, source: str, ts: Optional[float] = None) -> bool:
        """Append one outcome (J§6.3). Never merged into the decision record."""
        if self.recorder is None or not decision_id:
            return False
        return self._record({"record": "policy_outcome", "decision_id": decision_id,
                             "outcome_id": outcome_id, "value": float(value),
                             "ts": self._wall() if ts is None else ts,
                             "source": source})

    def _record(self, rec: dict) -> bool:
        try:
            ok = bool(self.recorder.record(rec))
        except Exception as e:  # noqa: BLE001 — recording never fails a request
            ok = False
            self._log(f"[policy] record refused: {e}")
        if not ok:
            with self._lock:
                self.records_dropped_here += 1
        return ok

    def close(self) -> None:
        if self.recorder is not None:
            self.recorder.close()

    # -- publishing (design §6; the routes are thin wrappers) ----------------------
    def publish(self, role: str, body: Any) -> dict:
        """Validate and atomically write an artifact (``role="active"``) or up to
        two (``role="shadow"``, a list). The previous active file is kept as
        ``.previous.json``. Raises :class:`PolicyRouteError` (503 without a
        validator, 422 listing every violation); writes nothing unless every
        artifact in the body is valid."""
        if role not in ("active", "shadow"):
            raise PolicyRouteError(400, f"role must be active or shadow, got {role!r}")
        if self.validator is None:
            raise PolicyRouteError(503, "cannot validate artifact without livestack_policy")
        arts = body if role == "shadow" else [body]
        if role == "shadow" and (not isinstance(body, list) or len(body) > MAX_SHADOWS):
            raise PolicyRouteError(422, {"violations": [{
                "code": "shadow_invalid",
                "detail": f"role=shadow takes a JSON array of at most {MAX_SHADOWS} artifacts"}]})
        violations, versions = [], []
        for i, art in enumerate(arts):
            where = f"[{i}] " if role == "shadow" else ""
            version, found = self.validator(_json.dumps(art))
            found = list(found)
            if isinstance(art, dict) and art.get("policy_id") != POLICY_ID:
                found.append({"code": "policy_id_mismatch",
                              "detail": f"policy_id {art.get('policy_id')!r}, "
                                        f"this route serves {POLICY_ID}"})
            violations += [{**v, "detail": where + str(v.get("detail", ""))} for v in found]
            versions.append(version)
        if violations:
            raise PolicyRouteError(422, {"violations": violations})
        _os.makedirs(self.policy_dir, exist_ok=True)
        with self._lock:
            previous = None
            if role == "active":
                cur = self._path("active")
                if _os.path.exists(cur):
                    with open(cur, "rb") as fh:
                        _atomic_write(self._path("previous"), fh.read())
                    previous = self._active.version if self._active else None
                _atomic_write(cur, _json.dumps(body, sort_keys=True).encode())
            else:
                _atomic_write(self._path("shadow"), _json.dumps(body, sort_keys=True).encode())
        self.reload_if_changed(force=True)
        return {"policy_id": POLICY_ID, "role": role,
                "version": versions[0] if role == "active" else versions,
                "previous_version": previous}

    def revert(self) -> dict:
        """Swap ``.previous.json`` back into ``.active.json`` (the old active
        becomes the previous, so a revert can itself be reverted). A file swap,
        by design: no model, no improver, nothing that can be locked out."""
        with self._lock:
            prev, cur = self._path("previous"), self._path("active")
            if not _os.path.exists(prev):
                raise PolicyRouteError(409, "no previous artifact to revert to")
            with open(prev, "rb") as fh:
                old = fh.read()
            current = None
            if _os.path.exists(cur):
                with open(cur, "rb") as fh:
                    current = fh.read()
            _atomic_write(cur, old)
            if current is not None:
                _atomic_write(prev, current)
            else:
                _os.remove(prev)
        self._log(f"[policy] reverted {POLICY_ID} to its previous artifact")
        self.reload_if_changed(force=True)
        return {"policy_id": POLICY_ID, "version": self.artifact_version,
                "previous_version": self._previous_version}

    # -- status --------------------------------------------------------------------
    def status(self) -> dict:
        """For ``GET /fleet`` and ``GET /fleet/policy/{id}`` (design §6, §7)."""
        art = self.active
        stats = None
        if self.recorder is not None:
            try:
                stats = dict(self.recorder.stats())
            except Exception as e:  # noqa: BLE001
                stats = {"last_error": f"stats failed: {e}"}
        exploration = "disabled"
        if art.exploring:
            if self.mode == "auto" and art.handle is not None:
                exploration = "enabled"
            elif self.native is None:
                exploration = "exploration_disabled:native_unavailable"
            else:
                exploration = f"disabled:mode_{self.mode}"
        degraded: List[str] = []
        if self._active is None:
            degraded.append("policy_artifact_missing")
        if self.last_load_error:
            degraded.append("policy_artifact_invalid")
        if self.mismatches > 0:
            degraded.append("policy_native_mismatch")
        if self.mode != "0" and art.handle is None:
            # No module, or (defaults only) a module that cannot version them.
            degraded.append("policy_native_unavailable")
        if exploration == "exploration_disabled:native_unavailable":
            degraded.append(exploration)
        if self.recorder is None:
            degraded.append("policy_records_unavailable")
        elif stats is not None:
            if (stats.get("dropped") or 0) > 0 or self.records_dropped_here > 0:
                degraded.append("policy_records_dropped")
            if stats.get("last_error"):
                degraded.append("policy_records_error")
        return {
            "policy_id": POLICY_ID, "family": {"id": FAMILY[0], "version": FAMILY[1]},
            "source": "file" if self._active is not None else "defaults",
            "active": art.summary(),
            "previous": {"version": self._previous_version},
            "shadow": [{"version": s.version} for s in self._shadows],
            "native": self.native is not None, "mode": self.mode,
            "exploration": exploration,
            "mismatches": self.mismatches, "last_load_error": self.last_load_error,
            "skipped_no_choice": self.skipped_no_choice,
            "records": {"stream": self.records_dir, "stats": stats,
                        "unavailable": self.records_unavailable_reason
                        if self.recorder is None else None},
            "degraded": degraded,
        }

    @classmethod
    def from_env(cls, log: Callable[[str], None] = lambda *_: None,
                 env: Optional[Mapping[str, str]] = None, **kw) -> "PolicyRuntime":
        e = _os.environ if env is None else env
        age = (e.get("LIVESTACK_POLICY_RECORDS_AGE_DAYS") or "").strip()
        return cls(
            e.get("LIVESTACK_POLICY_DIR") or default_policy_dir(),
            mode=(e.get("LIVESTACK_POLICY_NATIVE") or "auto").strip(),
            self_principals=[p.strip() for p in
                             (e.get("LIVESTACK_POLICY_SELF_PRINCIPALS") or "").split(",")
                             if p.strip()],
            log=log,
            records_max_mb=float(e.get("LIVESTACK_POLICY_RECORDS_MAX_MB") or 128),
            records_files=int(e.get("LIVESTACK_POLICY_RECORDS_FILES") or 16),
            # Unset means the age window is DISABLED (a delete-shaped bound
            # never defaults to deleting).
            records_age_days=int(age) if age else None, **kw)


class PolicyRouteError(Exception):
    """A refusal from ``publish``/``revert`` with the HTTP status it maps to."""

    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def _atomic_write(path: str, data: bytes) -> None:
    """Write temp, fsync, rename: a reader sees the old file or the new one."""
    tmp = f"{path}.tmp.{_os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        _os.fsync(fh.fileno())
    _os.replace(tmp, path)


class _GreedyView:
    def __init__(self, rt: PolicyRuntime):
        self.rt = rt

    def decide(self, ctx: dict, candidates: List[dict], decision_id: str) -> dict:
        return self.rt.decide(ctx, candidates, decision_id, explore=False)
