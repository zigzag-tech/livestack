#!/usr/bin/env python3
"""H09: real CUDA Laya-multilingual on pinned local weights. No CPU fallback."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

os.environ.setdefault("USE_TF", "0")

import torch
import laya

MODEL = os.environ.get("LAYA_MODEL_DIR", "/home/ubuntu/models/laya-multilingual")
OUT = sys.argv[1] if len(sys.argv) > 1 else "-"


def require_cuda():
    if not torch.cuda.is_available():
        raise SystemExit("CUDA unavailable; refusing CPU fallback")
    name = torch.cuda.get_device_name(0)
    if "NVIDIA" not in name and "GeForce" not in name and "RTX" not in name:
        # still a CUDA device
        pass
    return name


def mem():
    torch.cuda.synchronize()
    return {
        "allocated_bytes": int(torch.cuda.memory_allocated()),
        "reserved_bytes": int(torch.cuda.memory_reserved()),
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
    }


def timed_predict(agent, state, questions):
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    result = agent.predict(state, questions)
    torch.cuda.synchronize()
    ms = (time.perf_counter() - t0) * 1000
    return result, ms, mem()


def main():
    device = require_cuda()
    t_load = time.perf_counter()
    agent = laya.load(MODEL)
    if hasattr(agent, "model") and hasattr(agent.model, "to"):
        agent.model.to("cuda:0")
        if str(getattr(agent, "device", "cuda")) != "cuda":
            # Agent may already be on cuda
            pass
    torch.cuda.synchronize()
    load_ms = (time.perf_counter() - t_load) * 1000
    if str(getattr(agent, "device", "cuda")).startswith("cpu"):
        raise SystemExit("laya loaded on CPU; refusing")

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
        "model_revision": "convaiinnovations/laya-multilingual",
        "device": device,
        "backend": "cuda",
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
