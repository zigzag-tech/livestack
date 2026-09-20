#!/usr/bin/env python3
"""H09 parity: compare a CUDA and an MLX report produced by the h09_*_laya.py
scripts (task 4.4). Prints a verdict and exits non-zero when parity fails.

Parity is BOTH numerical and semantic, per delegation packet D: a numeric
tolerance alone would pass a pair whose argmax disagrees, and an argmax check
alone would pass a pair whose probabilities are far enough apart to land on
opposite sides of a policy threshold. Both are checked, and the policy
threshold is applied to the noul rows so the report says what the disagreement
COSTS rather than only that it exists.
"""
from __future__ import annotations

import json
import sys

TOLERANCE = 0.01
NOUL_THRESHOLD = 0.65  # C01 final policy admission probability


def main() -> int:
    cuda = json.load(open(sys.argv[1]))
    mlx = json.load(open(sys.argv[2]))
    out_path = sys.argv[3] if len(sys.argv) > 3 else None

    findings = []

    # Semantic: argmax agreement on each choice case.
    semantic = {}
    for case in ("short", "max_context"):
        c, m = cuda[case]["choice"], mlx[case]["choice"]
        semantic[case] = {"cuda": c, "mlx": m, "agree": c == m}
        if c != m:
            findings.append(f"{case}: argmax disagrees ({c} vs {m})")

    # Numerical: per-label probability deltas on the short case.
    deltas = {}
    for label, cv in cuda["short"]["probabilities"].items():
        mv = float(mlx["short"]["probabilities"].get(label, 0.0))
        d = abs(float(cv) - mv)
        deltas[label] = round(d, 4)
        if d > TOLERANCE:
            findings.append(f"short P({label}): delta {d:.4f} exceeds {TOLERANCE}")

    # Numerical + policy consequence on the noul rows.
    noul = {}
    for cid, cv in cuda["chip_50"]["sample"].items():
        mv = float(mlx["chip_50"]["sample"].get(cid, 0.0))
        cv = float(cv)
        d = abs(cv - mv)
        c_adm, m_adm = cv >= NOUL_THRESHOLD, mv >= NOUL_THRESHOLD
        noul[cid] = {
            "cuda": cv, "mlx": mv, "delta": round(d, 4),
            "cuda_admits": c_adm, "mlx_admits": m_adm,
            "same_decision": c_adm == m_adm,
        }
        if d > TOLERANCE:
            findings.append(f"noul {cid}: delta {d:.4f} exceeds {TOLERANCE}")
        if c_adm != m_adm:
            findings.append(
                f"noul {cid}: SAME policy yields opposite slates "
                f"(cuda {'admits' if c_adm else 'drops'}, mlx {'admits' if m_adm else 'drops'})")

    report = {
        "tolerance": TOLERANCE,
        "noul_threshold": NOUL_THRESHOLD,
        "cuda_report": {"device": cuda["device"], "model_revision": cuda["model_revision"]},
        "mlx_report": {"device": mlx["device"], "model_revision": mlx["model_revision"]},
        "semantic": semantic,
        "short_probability_deltas": deltas,
        "noul": noul,
        "latency_ms": {
            "load": {"cuda": cuda["load_ms"], "mlx": mlx["load_ms"]},
            "short": {"cuda": cuda["short"]["ms"], "mlx": mlx["short"]["ms"]},
            "max_context": {"cuda": cuda["max_context"]["ms"], "mlx": mlx["max_context"]["ms"]},
            "chip_50": {"cuda": cuda["chip_50"]["ms"], "mlx": mlx["chip_50"]["ms"]},
        },
        "findings": findings,
        "verdict": "pass" if not findings else "fail",
    }
    text = json.dumps(report, indent=2)
    if out_path:
        open(out_path, "w").write(text + "\n")
    print(text)
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
