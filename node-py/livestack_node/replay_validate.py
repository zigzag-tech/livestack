"""replay_validate.py — does the composition queueing model match what vLLM did?

    python -m livestack_node.replay_validate [--hours 24] [--unit llm_general] [--json PATH]

The composer scores compositions by replaying the demand log through
`composition_replay` (a KV pool, a batch cap, adapter slots). That model had
never been checked against the engine it models. This replays the demand log
of one engine lifetime through the live composition and samples the model at
the timestamps of vLLM's own 10-second stats lines (`Running / Waiting /
Deferred / GPU KV cache usage`), then reports how often the two agree.

Restricted to whole engine lifetimes (between `vLLM ready` and the next
`stopping vLLM`), because the engine's counters and queue start empty at each
start, and to records stamped with the composition that engine ran.

Pure parsing and scoring live in functions with no I/O; `main` reads the
journal, the demand log and the measured-cost store.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import composition_replay as rp
from .demand_log import read_demand

_STATS = re.compile(r"Running: (?P<run>\d+) reqs, Waiting: (?P<wait>\d+) reqs"
                    r"(?:, Deferred: (?P<deferred>\d+) reqs)?, GPU KV cache usage: (?P<kv>[\d.]+)%")
_READY = re.compile(r"\[harmony-llm\] vLLM ready: (?P<unit>\S+)")
_STOP = re.compile(r"\[harmony-llm\] stopping vLLM: (?P<unit>\S+)")


def parse_journal(lines: Iterable[str], unit: str) -> Tuple[List[dict], List[Tuple[float, float]]]:
    """(stats samples, engine lifetimes) from `journalctl -o short-unix` lines.
    A lifetime still running at the end of the lines ends at +inf."""
    samples, segments, start = [], [], None
    for line in lines:
        head, _, rest = line.partition(" ")
        try:
            t = float(head)
        except ValueError:
            continue
        m = _STATS.search(rest)
        if m:
            samples.append({"t": t, "running": int(m["run"]), "waiting": int(m["wait"]),
                            "deferred": int(m["deferred"]) if m["deferred"] is not None else None,
                            "kv": float(m["kv"]) / 100.0})
            continue
        m = _READY.search(rest)
        if m and m["unit"] == unit:
            # A `systemctl restart` kills harmony-llm before it can log
            # "stopping vLLM", so a second ready with no stop between them
            # ends the previous lifetime here, rather than overwriting it.
            if start is not None:
                segments.append((start, t))
            start = t
            continue
        m = _STOP.search(rest)
        if m and m["unit"] == unit and start is not None:
            segments.append((start, t))
            start = None
    if start is not None:
        segments.append((start, float("inf")))
    return samples, segments


def _confusion(pairs: Iterable[Tuple[bool, bool]]) -> dict:
    tp = fp = fn = tn = 0
    for actual, sim in pairs:
        if actual and sim:
            tp += 1
        elif sim:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None}


def compare(samples: Sequence[dict], records: Sequence[Mapping], *, kv_tokens: int,
            max_num_seqs: int, max_loras: int) -> dict:
    """Replay `records` and score the model against `samples` (same lifetime)."""
    rate = rp.fit_rate(records)
    if rate is None or not samples:
        return {"error": "no records with token counts and elapsed time" if rate is None
                else "no stats samples in the window"}
    jobs = rp.jobs_from_records(records, rate)
    res = rp.replay(jobs, kv_tokens=kv_tokens, max_num_seqs=max_num_seqs,
                    max_loras=max_loras, sample_at=[s["t"] for s in samples])
    sim = {t: (run, wait, used) for t, run, wait, used in res.timeline}
    rows = []
    for s in samples:
        run, wait, used = sim[s["t"]]
        rows.append((s, run, wait, used / kv_tokens if kv_tokens else 0.0))
    n = len(rows)
    by_kv = collections.defaultdict(lambda: [0, 0])          # kv bucket -> [actual waiting, sim waiting]
    for s, _run, wait, _kv in rows:
        if s["waiting"] > 0:
            b = f"{int(s['kv'] * 10) * 10:02d}-{int(s['kv'] * 10) * 10 + 10}%"
            by_kv[b][0] += 1
            by_kv[b][1] += 1 if wait > 0 else 0
    return {
        "samples": n, "records": len(records),
        "records_with_n": sum(1 for r in records if r.get("n") is not None),
        "multi_sample_records": sum(1 for r in records if (r.get("n") or 1) > 1),
        "fitted_s_per_token": rate,
        "running": {"mae": round(sum(abs(s["running"] - run) for s, run, _, _ in rows) / n, 3),
                    "mean_actual": round(sum(s["running"] for s, *_ in rows) / n, 3),
                    "mean_model": round(sum(run for _, run, _, _ in rows) / n, 3),
                    "max_actual": max(s["running"] for s, *_ in rows),
                    "max_model": max(run for _, run, _, _ in rows)},
        "waiting_any": _confusion((s["waiting"] > 0, wait > 0) for s, _, wait, _ in rows),
        "kv_usage": {"mae": round(sum(abs(s["kv"] - kv) for s, _, _, kv in rows) / n, 4),
                     "mean_actual": round(sum(s["kv"] for s, *_ in rows) / n, 4),
                     "mean_model": round(sum(kv for *_, kv in rows) / n, 4)},
        # Where the engine had requests waiting, by its own KV usage at the time,
        # and how many of those the model also had waiting. Waiting at low KV
        # usage is not a KV-pool effect: batch slots or prefill throughput.
        "actual_waiting_by_kv": {k: {"actual": a, "model_also": m}
                                 for k, (a, m) in sorted(by_kv.items())},
        "deferred_seen": sum(1 for s in samples if s.get("deferred")),
        "model": res.to_json(),
    }


def _composition(costs_path: str, chash: str) -> Optional[dict]:
    try:
        with open(costs_path, "r", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("composition_hash") == chash:
                    return row
    except FileNotFoundError:
        return None
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--unit", default="llm_general")
    ap.add_argument("--service", default="harmony-llm")
    host = os.environ.get("HARMONY_LLM_HOST_ID", "xc-tower-ubuntu-gpu1")
    cache = os.path.join(os.path.expanduser("~"), ".cache", "livestack")
    ap.add_argument("--demand-log", default=os.path.join(cache, "demand", f"{host}.jsonl"))
    ap.add_argument("--costs", default=os.path.join(cache, "unit-costs.jsonl"))
    ap.add_argument("--json", default=None, help="also write the report here")
    a = ap.parse_args(argv)
    now = time.time()
    since = now - a.hours * 3600
    out = subprocess.run(["journalctl", "-u", a.service, "--since", f"@{since:.0f}",
                          "--no-pager", "-o", "short-unix"], capture_output=True, text=True)
    samples, segments = parse_journal(out.stdout.splitlines(), a.unit)
    records = [r for r in read_demand(a.demand_log, since) if r.get("unit") == a.unit
               and r.get("outcome") == "ok"]
    report = {"window": {"since": since, "until": now, "hours": a.hours},
              "lifetimes": []}
    for lo, hi in segments:
        seg_records = [r for r in records if lo <= r["ts"] < hi]
        seg_samples = [s for s in samples if lo <= s["t"] < hi]
        hashes = collections.Counter(r.get("composition_hash") for r in seg_records)
        entry = {"from": lo, "to": hi if hi != float("inf") else None}
        if not hashes:
            entry["error"] = "no demand records in this lifetime"
            report["lifetimes"].append(entry)
            continue
        chash = hashes.most_common(1)[0][0]
        row = _composition(a.costs, chash) or {}
        comp = row.get("composition") or {}
        if not row or not row.get("kv_tokens"):
            entry["error"] = f"no measured cost for {chash}"
            report["lifetimes"].append(entry)
            continue
        seg_records = [r for r in seg_records if r.get("composition_hash") == chash]
        entry.update(composition_hash=chash, kv_tokens=row["kv_tokens"],
                     max_num_seqs=comp.get("max_num_seqs"), max_loras=len(comp.get("adapters") or []))
        entry.update(compare(seg_samples, seg_records, kv_tokens=int(row["kv_tokens"]),
                             max_num_seqs=int(comp.get("max_num_seqs") or 0) or 1 << 30,
                             max_loras=len(comp.get("adapters") or [])))
        report["lifetimes"].append(entry)
    text = json.dumps(report, indent=2)
    print(text)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
