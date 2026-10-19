#!/usr/bin/env python3
"""A trained label head for Simple Jev on Flash-Next: JEMM's idea without a LoRA.

    python scripts/flash_label_head.py build <raw-dir> <examples.jsonl>
    python scripts/flash_label_head.py collect <examples.jsonl> <out.jsonl> [--url URL]
    python scripts/flash_label_head.py fit <collected.jsonl> [--pane-status FILE] [--out PATH --served-model ID]

JEMM is a LoRA on the 27B trained on public decision sets. Flash-Next runs on
Strata, which loads no adapters and serves a 2-bit pack, so the LoRA cannot be
reproduced there. What can be trained is the last step: Strata's
`strata_hidden` (zigzag-tech/Strata zz/logprobs, 82b9f878) returns the vector
the head reads at the label position, and a small correction to the label
logits is learned from it:

    z_j = logprob_j + (U h)_j + c_j        j = the question's candidate labels

U is zero at the start, so the head starts at plain Simple Jev and only moves
where the data says so. `c` alone (position biases) is fitted as an ablation,
to tell content from debiasing.

build: JEMM's open sets as Simple Jev choice questions, candidates shuffled.
  CLINC150 "plus" (clinc-plus-{train,test}-*.parquet: 8 intents + "none"),
  Banking77 (banking-{train,test}.jsonl, mteb mirror: 8 intents),
  When2Call (when2call-mcq.jsonl: its 4 responses; split by uuid),
  Aegis 2.0 (aegis-{train,test}.json: the user prompt, safe or unsafe).
  750 train and 150 eval questions each, seed 20261018.
collect: one call per question to the Strata server (stdlib only), resumable.
fit: numpy. λ is chosen on a 15% slice of train; eval is scored once. With
  --pane-status (flash_hidden_probe.py's collected final set) the head is also
  scored on pane-status, a task it never saw. --out also writes <out>-head.npz,
  the file a unit's `decision_heads` registers (livestack_node/decisions/
  label_head.py); --served-model is the reply `model` it was collected from.

Pilot, 2026-10-18 (zz-whitby, Flash-Next Q2_0, 3,000 train / 600 eval, λ 0.01):

  eval accuracy        Simple Jev   biases only   head
  When2Call               0.700        0.687      0.793
  Banking77               0.920        0.927      0.933
  CLINC150                0.967        0.947      0.953
  Aegis                   0.833        0.833      0.800
  all four                0.855        0.848      0.870
  pane-status final       0.890        0.879      0.725   (robust: right in both orders; never trained on)

A head trained on these sets does not generalise: its gain is When2Call's, the
rest is noise at 150 questions a set, and it costs an unseen task 17 points.
So no general head is registered. A head trained on the task it serves does
help (benchday ml/eval/flash_hidden_probe.py: pane-status final robust 0.890 ->
0.912), so heads are per use: one per question family, named in options.head.
"""
import argparse
import base64
import hashlib
import json
import os
import random
import struct
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from livestack_node.decisions.simple_jev import CHOICE_LABELS, compile_branch  # noqa: E402

N_TRAIN, N_EVAL, SEED = 750, 150, 20261018
L = len(CHOICE_LABELS)


def _intent_questions(rows, names, k, none, rng, n):
    """rows: (text, intent name or None for out of scope)."""
    out = []
    for text, gold in rng.sample(rows, n):
        pool = [x for x in names if x != gold]
        keys = rng.sample(pool, k - (gold is not None)) + ([gold] if gold is not None else [])
        rng.shuffle(keys)
        crit = {x: x.replace("_", " ") for x in keys}
        if none:
            crit["none"] = "The request matches none of the other intents."
        out.append({"state": {"utterance": text}, "instructions": "Which intent does this request express?",
                    "criteria": crit, "gold": gold if gold is not None else "none"})
    return out


def build(raw, out_path):
    import pyarrow.parquet as pq
    rng = random.Random(SEED)
    qs = []

    def clinc(split):
        t = pq.read_table(os.path.join(raw, f"clinc-plus-{split}-00000-of-00001.parquet"))
        names = json.loads(t.schema.metadata[b"huggingface"])["info"]["features"]["intent"]["names"]
        rows = [(r["text"], None if names[r["intent"]] == "oos" else names[r["intent"]]) for r in t.to_pylist()]
        return rows, [x for x in names if x != "oos"]
    for split, n in (("train", N_TRAIN), ("test", N_EVAL)):
        rows, names = clinc(split)
        qs += [dict(q, set="clinc", split=split) for q in _intent_questions(rows, names, 8, True, rng, n)]

    for split, n in (("train", N_TRAIN), ("test", N_EVAL)):
        rows = [json.loads(l) for l in open(os.path.join(raw, f"banking-{split}.jsonl")) if l.strip()]
        names = sorted({r["label_text"] for r in rows})
        rows = [(r["text"], r["label_text"]) for r in rows]
        qs += [dict(q, set="banking77", split=split) for q in _intent_questions(rows, names, 8, False, rng, n)]

    w2c = [json.loads(l) for l in open(os.path.join(raw, "when2call-mcq.jsonl")) if l.strip()]
    for split, n in (("train", N_TRAIN), ("test", N_EVAL)):
        rows = [r for r in w2c if (int(hashlib.sha256(r["uuid"].encode()).hexdigest(), 16) % 5 == 0) == (split == "test")]
        for r in rng.sample(rows, n):
            tools = [json.loads(t) for t in r["tools"]]
            keys = list(r["answers"])
            rng.shuffle(keys)
            qs.append({"set": "when2call", "split": split,
                       "state": {"user_request": r["question"],
                                 "available_tools": {t["name"]: t.get("description", "") for t in tools}},
                       "instructions": "Which response should the assistant give?",
                       "criteria": {k: r["answers"][k] for k in keys}, "gold": r["correct_answer"]})

    crit = {"safe": "The prompt is safe to engage with: it seeks or expresses nothing harmful.",
            "unsafe": "The prompt is unsafe: it seeks or expresses harmful content such as violence, crime, "
                      "hate, harassment, self-harm or sexual content."}
    for split, n in (("train", N_TRAIN), ("test", N_EVAL)):
        rows = [r for r in json.load(open(os.path.join(raw, f"aegis-{split}.json")))
                if r.get("prompt") and r["prompt"] != "REDACTED" and r.get("prompt_label") in crit]
        for r in rng.sample(rows, n):
            keys = list(crit)
            rng.shuffle(keys)
            qs.append({"set": "aegis", "split": split, "state": {"user_prompt": r["prompt"]},
                       "instructions": "Is this user prompt safe or unsafe to engage with?",
                       "criteria": {k: crit[k] for k in keys}, "gold": r["prompt_label"]})

    with open(out_path, "w") as f:
        for i, q in enumerate(qs):
            f.write(json.dumps(dict(q, id=i)) + "\n")
    print(f"{len(qs)} questions", file=sys.stderr)


def collect(examples, out_path, url):
    done = {json.loads(l)["id"] for l in open(out_path)} if os.path.exists(out_path) else set()
    with open(out_path, "a") as out:
        for q in map(json.loads, open(examples)):
            if q["id"] in done:
                continue
            payload = {"state": q["state"], "questions": {"q": {
                "type": "choice", "instructions": q["instructions"], "criteria": q["criteria"]}}}
            messages, plan = compile_branch(payload, "q")
            body = {"model": "flash_next", "messages": messages, "continue_final_message": True,
                    "add_generation_prompt": False, "chat_template_kwargs": {"enable_thinking": False},
                    "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 20,
                    "strata_hidden": True}
            t0 = time.perf_counter()
            try:
                req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    res = json.load(r)
            except Exception as e:
                print(f"question {q['id']}: FAILED {e}", file=sys.stderr)
                continue
            alts = {a["token"]: a["logprob"] for a in res["choices"][0]["logprobs"]["content"][0]["top_logprobs"]}
            floor = min(alts.values())   # Strata ignores structured_outputs: a label past the top 20 is below this
            out.write(json.dumps({
                "id": q["id"], "set": q["set"], "split": q["split"], "n": len(plan["labels"]),
                "gold": list(q["criteria"]).index(q["gold"]),
                "logprobs": [alts.get(l, floor) for l in plan["labels"]],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "prompt_tokens": res["usage"]["prompt_tokens"],
                "hidden": res["choices"][0]["strata_hidden"]["b64"]}) + "\n")
            out.flush()


def _vec(b64):
    raw = base64.b64decode(b64)
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def _arrays(recs):
    import numpy as np
    X = np.array([_vec(r["hidden"]) for r in recs])
    LP = np.full((len(recs), L), -np.inf)
    for k, r in enumerate(recs):
        LP[k, :r["n"]] = r["logprobs"]
    y = np.array([r["gold"] for r in recs])
    return X, LP, y


def _train(Z, LP, y, lam, bias_only=False, steps=400, lr=0.01):
    """Adam on the candidates' softmax cross-entropy; U starts at zero."""
    import numpy as np
    U = np.zeros((Z.shape[1], L)); c = np.zeros(L)
    m = [np.zeros_like(U), np.zeros_like(c)]; v = [np.zeros_like(U), np.zeros_like(c)]
    for t in range(1, steps + 1):
        z = LP + c + (0 if bias_only else Z @ U)
        z -= z.max(1, keepdims=True)
        p = np.exp(z); p /= p.sum(1, keepdims=True)
        p[np.arange(len(y)), y] -= 1
        p /= len(y)
        grads = [np.zeros_like(U) if bias_only else Z.T @ p + lam * U, p.sum(0)]
        for i, (prm, g) in enumerate(zip((U, c), grads)):
            m[i] = 0.9 * m[i] + 0.1 * g; v[i] = 0.999 * v[i] + 0.001 * g * g
            prm -= lr * (m[i] / (1 - 0.9 ** t)) / (np.sqrt(v[i] / (1 - 0.999 ** t)) + 1e-8)
    return U, c


def _pred(Z, LP, U, c):
    return (LP + c + Z @ U).argmax(1)


def fit(collected, pane_status=None, out_path=None, served_model=None):
    import numpy as np
    recs = [json.loads(l) for l in open(collected) if l.strip()]
    train = [r for r in recs if r["split"] == "train"]
    evals = [r for r in recs if r["split"] == "test"]
    rng = random.Random(SEED)
    val_ids = {r["id"] for r in rng.sample(train, len(train) * 15 // 100)}
    fit_set = [r for r in train if r["id"] not in val_ids]
    val_set = [r for r in train if r["id"] in val_ids]
    Xf, LPf, yf = _arrays(fit_set)
    mu, sd = Xf.mean(0), Xf.std(0) + 1e-6
    z = lambda X: (X - mu) / sd
    Xv, LPv, yv = _arrays(val_set)
    zero = np.zeros((Xf.shape[1], L))
    sweep = {}
    for lam in (0.001, 0.01, 0.1, 1.0, 10.0, 100.0):
        U, c = _train(z(Xf), LPf, yf, lam)
        sweep[lam] = round(float((_pred(z(Xv), LPv, U, c) == yv).mean()), 4)
    lam = max(sweep, key=sweep.get)
    Xt, LPt, yt = _arrays(train)
    U, c = _train(z(Xt), LPt, yt, lam)
    _, cb = _train(z(Xt), LPt, yt, 0.0, bias_only=True)

    def per_set(rs):
        X, LP, y = _arrays(rs)
        arms = {"simple_jev": LP.argmax(1), "bias_only": (LP + cb).argmax(1), "head": _pred(z(X), LP, U, c)}
        res = {}
        for s in sorted({r["set"] for r in rs}) + ["all"]:
            idx = np.array([s in ("all", r["set"]) for r in rs])
            res[s] = {"n": int(idx.sum()), **{a: round(float((p[idx] == y[idx]).mean()), 4) for a, p in arms.items()}}
        return res

    out = {"rows": {"train": len(train), "eval": len(evals)}, "lambda": lam, "val_sweep": sweep,
           "eval": per_set(evals),
           "latency_ms_p50": sorted(r["ms"] for r in recs)[len(recs) // 2],
           "prompt_tokens_p50": sorted(r["prompt_tokens"] for r in recs)[len(recs) // 2]}
    if pane_status:
        out["pane_status_final"] = _pane_status(pane_status, z, U, c, cb)
    if out_path:
        if not served_model:
            raise SystemExit("--out needs --served-model: a head is only applied to the model it was trained on")
        json.dump(out, open(out_path, "w"), indent=2)
        meta = {"served_model": served_model, "dim": int(U.shape[0]), "lambda": lam, "trained_on": collected,
                "rows": len(train), "eval": out["eval"]["all"]}
        np.savez(os.path.splitext(out_path)[0] + "-head.npz", U=U, c=c, mu=mu, sd=sd, meta=json.dumps(meta))
    print(json.dumps(out, indent=2))


def _pane_status(path, z, U, c, cb):
    """flash_hidden_probe.py's records: one call per row and order, logprobs keyed by criterion, in the order asked.
    Scored as the hub reads it: "question" or not, against the gold `asking`."""
    import numpy as np
    recs = [json.loads(l) for l in open(path) if l.strip()]
    X = np.array([_vec(r["hidden"]) for r in recs])
    LP = np.full((len(recs), L), -np.inf)
    q_at = []
    for k, r in enumerate(recs):
        keys = list(r["logprobs"])                    # json keeps the order the criteria were asked in
        LP[k, :len(keys)] = [r["logprobs"][x] for x in keys]
        q_at.append(keys.index("question"))
    q_at = np.array(q_at)
    gold = np.array([r["label"] == "asking" for r in recs])
    res = {}
    for arm, pred in (("simple_jev", LP.argmax(1)), ("bias_only", (LP + cb).argmax(1)), ("head", _pred(z(X), LP, U, c))):
        said = pred == q_at
        by = {}
        for r, s, g in zip(recs, said, gold):
            by.setdefault(r["i"], {})[r["order"]] = s == g
        res[arm] = {"accuracy": round(float((said == gold).mean()), 4),
                    "robust": round(sum(all(v.values()) and len(v) == 2 for v in by.values()) / len(by), 4)}
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "collect", "fit"])
    ap.add_argument("args", nargs="+")
    ap.add_argument("--url", default="http://127.0.0.1:8188/v1/chat/completions")
    ap.add_argument("--pane-status")
    ap.add_argument("--out")
    ap.add_argument("--served-model")
    a = ap.parse_args()
    if a.cmd == "build":
        build(*a.args)
    elif a.cmd == "collect":
        collect(*a.args, a.url)
    else:
        fit(a.args[0], a.pane_status, a.out, a.served_model)
