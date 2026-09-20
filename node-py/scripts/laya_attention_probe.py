#!/usr/bin/env python3
"""Is zero-shot Laya able to do the pane-attention task at all?

This is a CAPABILITY probe, not a calibration and not Q01. Q01 scores sealed
production episodes; this asks a cheaper, prior question on hand-written
unambiguous cases: when a coding agent plainly asks the user a blocking
question, does `question` even win? If it cannot separate the clear cases, no
threshold on sealed data will rescue it, and calibrating on production
episodes first would spend real evidence to learn the same thing.

Cases are deliberately unambiguous and cover EN and ZH, because the profile
claims multilingual support. Each case names the label a careful human would
assign with no further context.
"""
from __future__ import annotations

import json
import os
import sys

CRITERIA = {
    "question": "A current blocking request for the user's answer or decision.",
    "working": "The agent is describing work it is continuing now.",
    "self_waiting": "The agent is waiting for its own tool, job, or external event.",
    "finished_turn": "The agent reports its turn concluded, without a blocking request.",
    "idle": "No current work or outstanding human request.",
    "unknown": "The supplied evidence cannot distinguish these states.",
}

CASES = [
    ("q-en-choice", "question", "Which database should I use, Postgres or SQLite?"),
    ("q-en-permission", "question", "I'm about to delete the migrations directory. Should I go ahead?"),
    ("q-en-clarify", "question", "Do you want me to fix the failing test, or revert the change?"),
    ("q-zh-choice", "question", "你想让我用 Postgres 还是 SQLite？请告诉我。"),
    ("q-zh-permission", "question", "我要删除这个目录了，确认继续吗？"),
    ("w-en-progress", "working", "Running the test suite now; I'll report the failures when it finishes."),
    ("w-en-editing", "working", "I'm editing hub/src/server.ts to add the new route."),
    ("w-zh-progress", "working", "我正在修改配置文件，稍后会继续跑测试。"),
    ("f-en-done", "finished_turn", "Done. The fix is committed and all 49 tests pass."),
    ("f-zh-done", "finished_turn", "完成了，代码已经提交，测试全部通过。"),
    ("s-en-waiting", "self_waiting", "Waiting for the CI run to finish before I can check the result."),
    ("s-en-build", "self_waiting", "The Docker build is still going; nothing for me to do until it lands."),
]


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "cuda"
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if backend == "cuda":
        import laya
        model = laya.load(os.environ.get("LAYA_MODEL_DIR", "/home/ubuntu/models/laya-multilingual"))
    else:
        import laya_mlx as laya
        model = laya.load(os.environ.get("LAYA_MODEL_DIR", "/Users/ubuntu/models/laya-multilingual-mlx"),
                          device="gpu")

    q = {"attention": {"type": "choice",
                       "instructions": "Classify the current assistant turn's human obligation.",
                       "criteria": CRITERIA}}
    rows, correct, q_recall_n, q_recall_hit = [], 0, 0, 0
    for cid, expect, text in CASES:
        r = model.predict({"current_agent_message": text}, q)
        a = r["answers"]["attention"]
        got, probs = a["choice"], a["probabilities"]
        ok = got == expect
        correct += ok
        if expect == "question":
            q_recall_n += 1
            q_recall_hit += ok
        rows.append({"id": cid, "expected": expect, "got": got, "ok": ok,
                     "p_expected": round(float(probs.get(expect, 0.0)), 4),
                     "p_question": round(float(probs.get("question", 0.0)), 4),
                     "rank_of_expected": 1 + sorted(probs.values(), reverse=True).index(probs[expect])})
    report = {
        "backend": backend,
        "n": len(CASES),
        "accuracy": round(correct / len(CASES), 4),
        "question_recall": round(q_recall_hit / q_recall_n, 4) if q_recall_n else None,
        "chance_accuracy": round(1 / len(CRITERIA), 4),
        "rows": rows,
    }
    text = json.dumps(report, indent=2)
    if out:
        open(out, "w").write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
