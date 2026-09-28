#!/usr/bin/env python3
"""Fit the JEMM template's temperature on THIS deployment's served model.

    python scripts/fit_jemm_calibration.py <pane-status-dev.jsonl> [--vllm URL] [--out PATH]

JEMM's card temperature was fitted on the bf16 base; Harmony serves an int4 one,
which shifts the logits. This asks the live `jemm` adapter every held-out row in
two candidate orders through the module's own compile path (so the fit matches
production rendering), then picks the temperature minimising the binary NLL of
P(question) against the gold `asking` label, the probability the hub thresholds.
The card's threshold is carried over unfitted and labelled so.

Dataset: the sealed pane-status dev set (canonical copy on zz-tower0,
~/chipgen/data/pane-status/sft-balanced/pane-status-dev.jsonl).
"""
import argparse
import ast
import datetime
import json
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from livestack_node.decisions import jemm  # noqa: E402

# Verbatim from benchday hub/src/decisions/jev-client.ts STATUS_CRITERIA.
CRITERIA = {
    "question": "The agent is blocking on the user's answer or decision right now.",
    "working": "The agent is describing work it is actively continuing.",
    "self_waiting": "The agent is waiting for its own tool, job, build or external event.",
    "finished_turn": "The agent reports its turn concluded, with no blocking request.",
    "idle": "No current work and no outstanding request.",
    "unknown": "The evidence cannot distinguish these states.",
}
INSTR = "Classify the current state of this coding-agent pane."


def state_from(row):
    raw = row.get("tail")
    turns = []
    if isinstance(raw, str):
        try:
            turns = ast.literal_eval(raw)
        except Exception:
            turns = []
    elif isinstance(raw, list):
        turns = raw
    lines = [f"{t.get('role', '?')}: {str(t.get('text') or '').strip()}"
             for t in turns[-8:] if isinstance(t, dict) and str(t.get("text") or "").strip()]
    return {"current_pane_screen": "\n".join(lines)[-3000:], "title": str(row.get("title") or "")[:200]}


def logits_for(vllm, state, criteria):
    q = {"type": "choice", "instructions": INSTR, "criteria": criteria}
    messages, plan = jemm.compile_question(state, q, max_options=20)
    body = jemm.request_body("local", messages, plan["labels"], "jemm", 20)
    req = urllib.request.Request(vllm, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    alts = {a["token"]: a["logprob"] for a in out["choices"][0]["logprobs"]["content"][0]["top_logprobs"]}
    return {ans: alts[lab] for lab, ans in zip(plan["labels"], plan["answers"])}


def p_question(logits, t):
    z = {k: v / t for k, v in logits.items()}
    m = max(z.values())
    w = {k: math.exp(v - m) for k, v in z.items()}
    return w["question"] / sum(w.values())


def nll(samples, t):
    eps = 1e-9
    return sum(-math.log(max(eps, p if y else 1 - p))
               for y, lg in samples for p in [p_question(lg, t)]) / len(samples)


def ece(samples, t, bins=10):
    buckets = [[] for _ in range(bins)]
    for y, lg in samples:
        p = p_question(lg, t)
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    n = len(samples)
    return sum(len(b) / n * abs(sum(p for p, _ in b) / len(b) - sum(y for _, y in b) / len(b))
               for b in buckets if b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--vllm", default="http://127.0.0.1:8189/v1/chat/completions")
    ap.add_argument("--out", default=jemm.CALIBRATION_PATH)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.dataset) if l.strip()]
    orders = [CRITERIA, dict(reversed(list(CRITERIA.items())))]
    samples = []
    for r in rows:
        st = state_from(r)
        for crit in orders:
            samples.append((r.get("label") == "asking", logits_for(a.vllm, st, crit)))
    card = jemm.calibration(jemm.CALIBRATION_PATH) if os.path.exists(jemm.CALIBRATION_PATH) else {}
    card_t = 1.3480874159655591
    grid = [round(0.25 + 0.05 * i, 2) for i in range(96)]          # 0.25 .. 5.0
    best_t = min(grid, key=lambda t: nll(samples, t))
    acc = sum((max(lg, key=lg.get) == "question") == y for y, lg in samples) / len(samples)
    report = {
        "n_rows": len(rows), "n_samples": len(samples), "positives": sum(y for y, _ in samples),
        "argmax_binary_accuracy": round(acc, 4),
        "card_temperature": {"t": card_t, "nll": round(nll(samples, card_t), 4), "ece": round(ece(samples, card_t), 4)},
        "fitted_temperature": {"t": best_t, "nll": round(nll(samples, best_t), 4), "ece": round(ece(samples, best_t), 4)},
    }
    print(json.dumps(report, indent=2))
    # ADOPT A REFIT ONLY WHEN IT CLEARLY WINS. On ~100 rows a grid minimum
    # moves with noise; the card's value is the prior, and it stays unless the
    # refit cuts NLL by at least 2% without worsening ECE.
    card_nll, fit_nll = report["card_temperature"]["nll"], report["fitted_temperature"]["nll"]
    adopt = fit_nll < card_nll * 0.98 and report["fitted_temperature"]["ece"] <= report["card_temperature"]["ece"]
    chosen_t = best_t if adopt else card_t
    out = {
        "id": (f"jemm-int4-{datetime.date.today().isoformat()}" if adopt
               else f"jemm-card-verified-int4-{datetime.date.today().isoformat()}"),
        "temperature": chosen_t,
        "temperature_source": ("refit on this deployment" if adopt else
                               "adapter card; a refit on this deployment did not clearly beat it"),
        "threshold": 0.9872681877423998,
        "threshold_source": "adapter card (bf16 base), NOT refit: 98 rows cannot fit a 0.99 tail",
        "source": (f"fitted by scripts/fit_jemm_calibration.py on {len(rows)} held-out pane-status rows x 2 "
                   f"candidate orders against the served int4 base (dbirks/Qwen3.8-27B-W4A16-AutoRound, "
                   f"fp8 KV); binary NLL of P(question) vs gold asking"),
        "fit_report": report,
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    main()
