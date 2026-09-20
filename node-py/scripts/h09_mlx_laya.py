#!/usr/bin/env python3
"""H09: real native MLX Laya-multilingual on pinned local weights.

Mirrors scripts/h09_cuda_laya.py case for case so the two reports can be
compared row by row for H09 parity (packet D acceptance, task 4.4). Any edit
here that changes a fixture MUST be mirrored there, or the comparison silently
stops comparing the same thing.

Refuses to run if torch is importable: a PyTorch-contaminated environment is
not the arm64/Metal runtime this report claims to measure.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import sys
import time

import mlx.core as mx
import laya_mlx as laya

MODEL = os.environ.get("LAYA_MODEL_DIR", "/Users/ubuntu/models/laya-multilingual-mlx")
OUT = sys.argv[1] if len(sys.argv) > 1 else "-"


def require_metal():
    try:
        import torch  # noqa: F401
    except ImportError:
        pass
    else:
        raise SystemExit("torch is importable; this is not a clean MLX install")
    if platform.machine() != "arm64":
        raise SystemExit(f"not arm64 ({platform.machine()}); refusing")
    if not mx.metal.is_available():
        raise SystemExit("Metal unavailable; refusing CPU fallback")
    return platform.processor() or "apple-silicon"


def mem():
    """Active/cache/peak are MLX's own; RSS is the host's. Reported separately
    on purpose — on unified memory, adding them double counts (task 4.3)."""
    mx.synchronize()
    return {
        "active_bytes": int(mx.get_active_memory()),
        "cache_bytes": int(mx.get_cache_memory()),
        "peak_bytes": int(mx.get_peak_memory()),
        "host_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
    }


def timed_predict(agent, state, questions):
    mx.reset_peak_memory()
    mx.synchronize()
    t0 = time.perf_counter()
    result = agent.predict(state, questions)
    mx.synchronize()
    ms = (time.perf_counter() - t0) * 1000
    return result, ms, mem()


def main():
    device = require_metal()
    t_load = time.perf_counter()
    agent = laya.load(MODEL, device="gpu")
    mx.synchronize()
    load_ms = (time.perf_counter() - t_load) * 1000

    attn_q = {
        "attention": {
            "type": "choice",
            "instructions": "Classify the current assistant turn's human obligation.",
            "criteria": {
                "question": "A current blocking request for the user's answer or decision.",
                "working": "The agent is describing work it is continuing now.",
                "self_waiting": "The agent is waiting for its own tool, job, or external event.",
                "finished_turn": "The agent reports its turn concluded, without a blocking request.",
                "idle": "No current work or outstanding human request.",
                "unknown": "The supplied evidence cannot distinguish these states.",
            },
        }
    }
    short_state = {
        "current_agent_message": "Which database should I use, Postgres or SQLite?",
        "preceding_user_message": "please use postgres",
    }
    max_state = {
        "current_agent_message": short_state["current_agent_message"],
        "preceding_user_message": short_state["preceding_user_message"],
        "history": ["older turn %d: " % i + ("x" * 80) for i in range(20)],
    }

    short, short_ms, short_mem = timed_predict(agent, short_state, attn_q)
    maxr, max_ms, max_mem = timed_predict(agent, max_state, attn_q)

    noul = {}
    for i in range(50):
        noul[f"c{i:02d}"] = {
            "type": "noul",
            "instructions": f"Is this candidate a useful next input? Candidate: cmd-{i} git status",
        }
    chips, chip_ms, chip_mem = timed_predict(agent, short_state, noul)

    def ids(result, expected):
        got = sorted(result["answers"])
        return got == sorted(expected), got

    short_ok, _ = ids(short, ["attention"])
    chip_ok, chip_ids = ids(chips, list(noul))
    probs = short["answers"]["attention"]["probabilities"]
    finite = all(0 <= float(v) <= 1 for v in probs.values())
    report = {
        "model": MODEL,
        "model_revision": "aac6fef/laya-multilingual-mlx",
        "device": device,
        "backend": "mlx",
        "load_ms": round(load_ms, 2),
        "short": {
            "ms": round(short_ms, 2),
            "choice": short["answers"]["attention"]["choice"],
            "probabilities": probs,
            "memory": short_mem,
            "id_set_ok": short_ok,
            "finite": finite,
        },
        "max_context": {
            "ms": round(max_ms, 2),
            "choice": maxr["answers"]["attention"]["choice"],
            "memory": max_mem,
        },
        "chip_50": {
            "ms": round(chip_ms, 2),
            "n": len(chip_ids),
            "id_set_ok": chip_ok,
            "memory": chip_mem,
            "sample": {k: chips["answers"][k].get("noul", chips["answers"][k].get("probability")) for k in list(noul)[:3]},
        },
        "memory_hash": hashlib.sha256(
            json.dumps({"short": short_mem, "max": max_mem, "chip": chip_mem}, sort_keys=True).encode()
        ).hexdigest(),
        "cpu_fallback": False,
    }
    text = json.dumps(report, indent=2)
    if OUT == "-":
        print(text)
    else:
        open(OUT, "w").write(text + "\n")
        print("wrote", OUT, "choice", report["short"]["choice"], "chip_n", report["chip_50"]["n"])


if __name__ == "__main__":
    main()
