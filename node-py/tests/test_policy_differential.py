"""Differential test: native `livestack.fleet.choose_target` vs the reference
(scheduler-policy-routine design §4, task 4.2; Jingway design §11).

10 000 seeded cases — random params inside the hard bounds (plus defaults and
corners), contexts and candidates drawn like the golden generator's — go
through `choose_target_reference` and through the native
`livestack_policy.policy.decide`. Per candidate: identical eligibility,
explorability and reason code (the text before the first space), scores equal
within 1e-12; and the same greedy choice. The cases are written as JSONL (to a
temp dir) so a failing run can be replayed; every mismatch is written to
`tests/policy_golden/mismatch-<n>.json` and fails the test.

The reference wraps `fleet_scheduler._feasible_candidates`/`_score`, so when
the two disagree the native side is the one that deviates from §2.4.
"""
from __future__ import annotations

import json
import os
import random
from collections import Counter

import pytest

from livestack_node.policy_runtime import (
    ARTIFACT_SCHEMA, DEFAULT_PARAMS, FAMILY, PARAM_BOUNDS, POLICY_ID,
    SCORE_TOLERANCE, choose_target_reference,
)

try:
    from livestack_policy import policy as native  # type: ignore
except ImportError:  # pragma: no cover - reported as a named skip
    native = None

pytestmark = pytest.mark.skipif(
    native is None,
    reason="livestack_policy not importable: build it with "
           "`maturin develop -m native/policy/py/Cargo.toml` (task 4.2)")

SEED = 20260924
N_CASES = 10_000
MAX_MISMATCH_FILES = 20
HERE = os.path.dirname(os.path.abspath(__file__))
MISMATCH_DIR = os.path.join(HERE, "policy_golden")

TIERS = ("LOCAL", "SPOT", "ONDEMAND", "LAST_RESORT")
SLAS = ("interactive", "normal", "batch")
HOSTS = ("tower0", "xc-tower", "mac", "aliyun", "runpod")
# As in policy_golden/generate.py: few distinct values, so ties in distance,
# utilization and cost (where normalisation and tie-break bugs show) are common.
DISTANCES = (None, None, 2.3, 16.2, 16.2, 120.0, 700.5)
UTILIZATIONS = (None, None, 0.0, 0.25, 0.5, 0.5, 1.0)
PER_HOUR = (0.0, 0.0, 0.5, 2.0, 20.0)
PER_JOB = (0.0, 0.0, 0.01, 0.3)
LATENCIES = (0.0, 10.0, 25.0, 90.0, 600.0, 4000.0)
NOW = 1_788_600_000.0


def _params(rng: random.Random) -> dict:
    shape = rng.random()
    if shape < 0.1:
        return dict(DEFAULT_PARAMS)
    out = {}
    for name, (lo, hi) in PARAM_BOUNDS.items():
        if shape < 0.2:  # corners: every param at a bound (zero weights tie everything)
            out[name] = float(rng.choice((lo, hi)))
        else:
            out[name] = lo + rng.random() * (hi - lo)
    return out


def _ctx(rng: random.Random, i: int) -> dict:
    created = NOW - rng.choice((0.0, 5.0, 29.0, 31.0, 600.0, 1790.0, 1810.0, 40000.0))
    deadline = None
    if rng.random() < 0.3:
        deadline = NOW + rng.choice((-5.0, 0.0, 9.0, 60.0, 3600.0))
    return {"now": NOW, "job": {
        "id": f"j{i}", "sla": rng.choice(SLAS), "created_at": created,
        "deadline": deadline, "est_duration_s": rng.choice((0.0, 5.0, 60.0, 3600.0)),
        "locality_host": rng.choice((None, None) + HOSTS)}}


def _candidate(rng: random.Random, i: int) -> dict:
    shape = rng.random()
    running = shape < 0.55
    elastic = (not running and shape < 0.92) or (running and rng.random() < 0.2)
    pool = (not running) and elastic
    return {"id": f"t{i}", "features": {
        "host_id": rng.choice(HOSTS), "tier": rng.choice(TIERS),
        "running": running, "elastic": elastic,
        "selector_match": rng.random() < 0.85,
        "fits_now": running and rng.random() < 0.8,
        "headroom_ok": pool and rng.random() < 0.8,
        "fits_instance": pool and rng.random() < 0.85,
        "provision_latency_s": 0.0 if running else rng.choice(LATENCIES),
        "cost_per_hour": rng.choice(PER_HOUR), "cost_per_job": rng.choice(PER_JOB),
        "distance_ms": rng.choice(DISTANCES), "utilization": rng.choice(UTILIZATIONS)}}


def cases(seed: int = SEED, n: int = N_CASES):
    rng = random.Random(seed)
    for i in range(n):
        # Mostly the golden generator's 0–12 targets; 5% up to Jingway §11's 64.
        k = rng.randint(0, 64) if rng.random() < 0.05 else rng.randint(0, 12)
        yield {"case": i, "params": _params(rng), "context": _ctx(rng, i),
               "candidates": [_candidate(rng, c) for c in range(k)]}


def _artifact(params: dict):
    art = {"schema": ARTIFACT_SCHEMA, "policy_id": POLICY_ID,
           "family": {"id": FAMILY[0], "version": FAMILY[1]},
           "version": "", "parent_version": None, "params": params,
           "exploration": {"enabled": False, "epsilon": 0.0, "margin": 0.0},
           "provenance": {"created_by": "code:differential",
                          "created_at": "2026-09-24T00:00:00Z"}}
    art["version"] = native.artifact_version(art)
    return native.load_artifact(json.dumps(art))


def _code(row) -> str:
    """The reason code: the text before the first space, with a scored row's
    formatted score dropped (scores are compared numerically, within 1e-12)."""
    head = row["reason"].split(" ", 1)[0]
    return "scored" if head.startswith("scored:") else head


def _greedy(rows):
    best, gid = None, None
    for r in rows:
        if r["eligible"] and (best is None or r["score"] < best):
            best, gid = r["score"], r["id"]
    return gid


def _diff(ref_rows, nat) -> list:
    out = []
    nat_rows = nat["rows"]
    if [r["id"] for r in ref_rows] != [r["id"] for r in nat_rows]:
        return ["row ids/order differ"]
    for a, b in zip(ref_rows, nat_rows):
        for k in ("eligible", "explorable"):
            if a[k] != b[k]:
                out.append(f"{a['id']}.{k}: ref {a[k]} native {b[k]}")
        if _code(a) != _code(b):
            out.append(f"{a['id']}.reason: ref {a['reason']!r} native {b['reason']!r}")
        if (a["score"] is None) != (b["score"] is None) or (
                a["score"] is not None and abs(a["score"] - b["score"]) > SCORE_TOLERANCE):
            out.append(f"{a['id']}.score: ref {a['score']!r} native {b['score']!r}")
    if _greedy(ref_rows) != nat["greedy"]:
        out.append(f"greedy: ref {_greedy(ref_rows)!r} native {nat['greedy']!r}")
    return out


def test_native_matches_reference_on_10000_cases(tmp_path):
    corpus = tmp_path / "cases.jsonl"
    mismatches = 0
    stats = {"eligible_rows": 0, "rows": 0, "no_choice": 0}
    codes: Counter = Counter()
    with corpus.open("w") as fh:
        for case in cases():
            fh.write(json.dumps(case) + "\n")
            ref = choose_target_reference(case["params"], case["context"], case["candidates"])
            nat = native.decide(_artifact(case["params"]), POLICY_ID, case["context"],
                                case["candidates"], f"diff-{case['case']}")
            stats["rows"] += len(ref)
            stats["eligible_rows"] += sum(r["eligible"] for r in ref)
            stats["no_choice"] += _greedy(ref) is None
            codes.update(_code(r) for r in ref)
            problems = _diff(ref, nat)
            if problems:
                mismatches += 1
                if mismatches <= MAX_MISMATCH_FILES:
                    path = os.path.join(MISMATCH_DIR, f"mismatch-{mismatches}.json")
                    with open(path, "w") as out:
                        json.dump({**case, "problems": problems, "reference_rows": ref,
                                   "native_rows": nat["rows"], "native_greedy": nat["greedy"]},
                                  out, indent=2)
    print(f"differential: {N_CASES} cases, {stats['rows']} rows "
          f"({stats['eligible_rows']} eligible), {stats['no_choice']} with no choice, "
          f"{mismatches} mismatches; reason codes {dict(sorted(codes.items()))}")
    # The corpus must exercise both sides of every branch, or agreement proves little.
    assert stats["eligible_rows"] > 10_000 and stats["no_choice"] > 100
    assert set(codes) == {"scored", "filtered:selector", "filtered:deadline", "filtered:no_room",
                          "filtered:pool_at_cap", "filtered:instance_too_small",
                          "filtered:cold_not_elastic", "filtered:last_resort_guard"}, codes
    assert mismatches == 0, f"{mismatches} mismatching cases; see {MISMATCH_DIR}/mismatch-*.json"
