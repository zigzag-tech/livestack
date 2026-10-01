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
_BLOCK = re.compile(r"Setting attention block size to (?P<tokens>\d+) tokens")
_READY = re.compile(r"\[harmony-llm\] vLLM ready: (?P<unit>\S+)")
_STOP = re.compile(r"\[harmony-llm\] stopping vLLM: (?P<unit>\S+)")


def parse_journal(lines: Iterable[str], unit: str) -> Tuple[List[dict], List[Tuple[float, float]]]:
    """(stats samples, engine lifetimes) from `journalctl -o short-unix` lines.
    A lifetime still running at the end of the lines ends at +inf. Block-size
    lines are returned as samples with `block_size` (the engine prints one per
    start, before it is ready); see `block_size_before`."""
    samples, segments, start = [], [], None
    for line in lines:
        head, _, rest = line.partition(" ")
        try:
            t = float(head)
        except ValueError:
            continue
        m = _BLOCK.search(rest)
        if m:
            samples.append({"t": t, "block_size": int(m["tokens"])})
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


def block_size_before(samples: Sequence[dict], t: float) -> int:
    """The block size the engine printed at its last start before `t`."""
    b = 0
    for s in samples:
        if "block_size" in s and s["t"] <= t:
            b = s["block_size"]
    return b


def fit_state_pages(samples: Sequence[dict], records: Sequence[Mapping], *, kv_tokens: int,
                    block_size: int) -> Optional[dict]:
    """State pages per running sequence, from the engine's own stats lines.

    Samples with nothing waiting: pages in use = usage x pool pages, regressed
    on running sequences. The slope is pages per sequence; subtracting the mean
    attention pages a sequence of this traffic needs leaves the per-sequence
    state. None when there is too little to fit (fewer than 20 samples with
    something running, or no usable records)."""
    pool = kv_tokens / block_size
    pts = [(s["running"], s["kv"] * pool) for s in samples
           if "running" in s and s["waiting"] == 0 and s["running"] > 0]
    jobs = rp.jobs_from_records(records, 1.0)
    if len(pts) < 20 or not jobs:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    seqs = sum(j.seqs for j in jobs)
    attn = sum(rp.kv_need(j, block_size, 0.0) / block_size for j in jobs) / seqs
    return {"state_pages_per_seq": round(max(0.0, slope - attn), 3),
            "slope_pages_per_seq": round(slope, 3), "intercept_pages": round(my - slope * mx, 3),
            "attention_pages_per_seq": round(attn, 3), "samples": n, "records": len(jobs)}


def quiet_records(samples: Sequence[dict], records: Sequence[Mapping]) -> List[Mapping]:
    """Records whose whole life (start to end, plus one stats interval) saw
    nothing waiting in the engine: their elapsed time is service, not queue."""
    st = [s for s in samples if "running" in s]
    ts = [s["t"] for s in st]
    import bisect
    out = []
    for r in records:
        if r.get("elapsed_ms") is None:
            continue
        a = bisect.bisect_left(ts, r["ts"])
        b = bisect.bisect_right(ts, r["ts"] + r["elapsed_ms"] / 1000.0 + 10.0)
        win = st[max(0, a - 1):b]
        if win and all(s["waiting"] == 0 for s in win):
            out.append(r)
    return out


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
            max_num_seqs: int, max_loras: int, block_size: int = 0,
            state_pages: float = 0.0, prefill_tok_s: float = 0.0,
            decode_tok_s: float = 0.0) -> dict:
    """Replay `records` and score the model against `samples` (same lifetime)."""
    samples = [s for s in samples if "running" in s]
    rate = rp.fit_rate(records)
    if rate is None or not samples:
        return {"error": "no records with token counts and elapsed time" if rate is None
                else "no stats samples in the window"}
    jobs = rp.jobs_from_records(records, rate, prefill_tok_s=prefill_tok_s,
                                decode_tok_s=decode_tok_s)
    res = rp.replay(jobs, kv_tokens=kv_tokens, max_num_seqs=max_num_seqs,
                    max_loras=max_loras, sample_at=[s["t"] for s in samples],
                    block_size=block_size, state_pages=state_pages,
                    prefill_tok_s=prefill_tok_s)
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
        "kv_accounting": (f"pages:{block_size}x{state_pages:g}" if block_size else "tokens")
                         + (f",service:{prefill_tok_s:.0f}/{decode_tok_s:.1f}" if prefill_tok_s
                            else ",service:blended"),
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
    ap.add_argument("--fit-state", action="store_true",
                    help="fit state pages per sequence from the longest lifetime and store "
                         "it (with the block size) on that composition's measured-cost row")
    ap.add_argument("--tokens", action="store_true",
                    help="replay with token accounting (the pre-2026-09-30 model), for comparison")
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
        block = int(row.get("block_size") or 0) or block_size_before(samples, lo + 600)
        spages = float(row.get("state_pages_per_seq") or 0.0)
        pre, dec = float(row.get("prefill_tok_s") or 0.0), float(row.get("decode_tok_s") or 0.0)
        entry.update(composition_hash=chash, kv_tokens=row["kv_tokens"], block_size=block,
                     max_num_seqs=comp.get("max_num_seqs"), max_loras=len(comp.get("adapters") or []))
        entry["_fit_inputs"] = (seg_samples, seg_records, row, block)
        use_pages = not a.tokens and block > 0 and spages > 0
        entry.update(compare(seg_samples, seg_records, kv_tokens=int(row["kv_tokens"]),
                             max_num_seqs=int(comp.get("max_num_seqs") or 0) or 1 << 30,
                             max_loras=len(comp.get("adapters") or []),
                             block_size=block if use_pages else 0,
                             state_pages=spages if use_pages else 0.0,
                             prefill_tok_s=0.0 if a.tokens else pre,
                             decode_tok_s=0.0 if a.tokens else dec))
        report["lifetimes"].append(entry)
    if a.fit_state:
        cands = [e for e in report["lifetimes"] if e.get("_fit_inputs") and e.get("block_size")]
        if not cands:
            report["fit"] = {"error": "no lifetime with a block size and records to fit"}
        else:
            best = max(cands, key=lambda e: e.get("samples", 0))
            seg_samples, seg_records, row, block = best["_fit_inputs"]
            fit = fit_state_pages(seg_samples, seg_records, kv_tokens=int(row["kv_tokens"]),
                                  block_size=block)
            rates = rp.fit_two_rate(quiet_records(seg_samples, seg_records))
            if fit is None or rates is None:
                report["fit"] = {"error": "too few samples or quiet records to fit"}
            else:
                from .demand_log import UnitCostStore
                fit.update(fitted_at=now, lifetime_from=best["from"], block_size=block,
                           prefill_tok_s=round(rates[0], 1), decode_tok_s=round(rates[1], 2))
                # A direct burst measurement (scripts/measure_kv_pages.py)
                # beats a traffic fit for state pages: keep it, and record the
                # fit beside it. Service rates come only from traffic.
                state = (row["state_pages_per_seq"] if row.get("state_burst")
                         else fit["state_pages_per_seq"])
                fit["state_pages_used"] = "state_burst" if row.get("state_burst") else "this fit"
                UnitCostStore(a.costs).put({**row, "block_size": block,
                                            "state_pages_per_seq": state,
                                            "prefill_tok_s": fit["prefill_tok_s"],
                                            "decode_tok_s": fit["decode_tok_s"],
                                            "service_source": "replay_validate --fit-state",
                                            "state_fit": fit})
                report["fit"] = fit
    for e in report["lifetimes"]:
        e.pop("_fit_inputs", None)
    text = json.dumps(report, indent=2)
    print(text)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
