"""composition_replay.py — would this composition have queued the demand we saw?

The cost of a composition is mostly queueing, and queueing on vLLM is decided by
three things only: the KV-token pool, the batch cap (`max_num_seqs`), and the
LoRA slots (`max_loras`). This module replays a demand trace through exactly
those three and nothing else (design §6). It is a MODEL, so it states what it
assumes and it is validated before it is trusted:

* **Admission reserves prompt + completion up front.** vLLM allocates KV blocks
  incrementally and can preempt; reserving the whole request at admission is
  the conservative envelope (it never admits more than vLLM could hold at the
  request's peak). The journal shows the cost of this simplification — see
  `tests/fixtures/composition/journal-llm_general-2026-09-28.json`: real vLLM
  queues at every KV decile, this model only when the pool is nearly full.
* **FCFS with head-of-line blocking.** vLLM's waiting queue is FCFS (priority
  scheduling only reorders within a class); a head that does not fit blocks
  those behind it, as it does here.
* **Service time is a per-token rate fitted from the trace** (`fit_rate`):
  Σ(elapsed − queue) / Σ(prompt + completion) per base model. A request holds
  its tokens for `tokens × rate`. That ignores batch-size-dependent slowdown;
  it is the simplest estimator whose inputs the demand log actually carries.
* **Unknown token counts are imputed, and counted.** A record with `null`
  tokens (a stream that ended without `usage`) gets its base's median, and
  `ReplayResult.imputed` says how many — an imputed count is never silent.
* **A request larger than the whole pool is `rejected`**, not queued forever.
  The feasibility rule `truncates:<n>` is what keeps such compositions out.

Pure: records in, numbers out. No clock — `now` is always an argument.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

WEEK_S = 7 * 24 * 3600.0
HOUR_S = 3600.0


def record_tokens(r: Mapping) -> Optional[int]:
    """prompt + completion, or None when either is unknown (never 0)."""
    p, c = r.get("prompt_tokens"), r.get("completion_tokens")
    if p is None or c is None:
        return None
    return int(p) + int(c)


def fit_rate(records: Iterable[Mapping]) -> Optional[float]:
    """Seconds of service per token, Σ(elapsed − queue) / Σ tokens.

    Only records with both token counts and an elapsed time count. `queue_ms`
    is subtracted when present so the live composition's own queueing is not
    baked into the service time (it is `null` today; design §3). Returns None
    when nothing in the trace can support a fit — the caller decides what an
    absent rate means, this function does not invent one."""
    secs = toks = 0.0
    for r in records:
        t = record_tokens(r)
        if not t or r.get("elapsed_ms") is None:
            continue
        secs += max(0.0, (float(r["elapsed_ms"]) - float(r.get("queue_ms") or 0.0)) / 1000.0)
        toks += t
    return secs / toks if toks > 0 else None


@dataclass(frozen=True)
class Job:
    """One replayed request. `counted` is False for self traffic: it still
    occupies the pool (it is still served) but its delay is not a cost."""
    ts: float
    tokens: int
    service_s: float
    adapter: Optional[str] = None
    counted: bool = True
    imputed: bool = False
    # Sequences the request runs as: its `n`. vLLM runs `n` parallel samples
    # as `n` sequences against `max_num_seqs` (and counts each in its request
    # metrics); the hub's chip generation sends n=12. `tokens` already holds
    # every sample's completion, because `usage` sums them.
    seqs: int = 1


@dataclass(frozen=True)
class ReplayResult:
    n: int
    total_queue_s: float            # counted jobs only
    p95_queue_s: float              # counted jobs only
    max_running: int
    max_waiting: int
    frac_time_waiting: float        # time-weighted, over [first arrival, last departure]
    swaps: int                      # adapter loads that had to evict a resident adapter
    rejected: int                   # larger than the whole KV pool
    imputed: int                    # token counts filled with the base median
    # Lowest KV usage (fraction of the pool, after the event) at which any
    # request was left waiting. 1.0 when nothing ever waited.
    min_kv_usage_when_waiting: float = 1.0
    delays: Tuple[float, ...] = field(default=(), repr=False)
    # (t, running sequences, waiting requests, KV tokens in use) at each of the
    # `sample_at` times: the state after every event BEFORE t, which is what an
    # engine's periodic stats line reports. Empty unless asked for.
    timeline: Tuple[Tuple[float, int, int, int], ...] = field(default=(), repr=False)

    def to_json(self) -> dict:
        return {"n": self.n, "total_queue_s": round(self.total_queue_s, 6),
                "p95_queue_s": round(self.p95_queue_s, 6), "max_running": self.max_running,
                "max_waiting": self.max_waiting,
                "frac_time_waiting": round(self.frac_time_waiting, 6), "swaps": self.swaps,
                "rejected": self.rejected, "imputed": self.imputed,
                "min_kv_usage_when_waiting": round(self.min_kv_usage_when_waiting, 6)}


def jobs_from_records(records: Sequence[Mapping], rate_s_per_token: float, *,
                      counted: Callable[[Mapping], bool] = lambda r: True) -> List[Job]:
    """Records -> jobs. Unknown token counts take the median of the known ones;
    if none are known the record cannot be modelled and is dropped (the
    caller sees it in `n`)."""
    known = sorted(t for t in (record_tokens(r) for r in records) if t is not None)
    median = known[len(known) // 2] if known else None
    out = []
    for r in records:
        t = record_tokens(r)
        imputed = t is None
        if imputed:
            if median is None:
                continue
            t = median
        out.append(Job(ts=float(r["ts"]), tokens=int(t), service_s=t * rate_s_per_token,
                       adapter=r.get("adapter"), counted=counted(r), imputed=imputed,
                       seqs=int(r.get("n") or 1)))
    return out


def replay(jobs: Sequence[Job], *, kv_tokens: int, max_num_seqs: int,
           max_loras: int, sample_at: Sequence[float] = ()) -> ReplayResult:
    """FCFS event simulation over the KV pool, batch cap and adapter slots.

    Adapter slots are LRU among adapters not in use by a running request; a
    base-model request (`adapter=None`) needs no slot. Loading into a free slot
    is not a swap; evicting a resident adapter to load another is."""
    order = sorted(range(len(jobs)), key=lambda i: (jobs[i].ts, i))
    running: list = []                 # heap of (end_ts, seq, job_index)
    waiting: list = []                 # FIFO of job indices
    used = 0
    running_seqs = 0                   # Σ seqs of running jobs (vLLM's "Running: N reqs")
    slots: dict = {}                   # adapter -> last-use ts (resident adapters)
    in_use: dict = {}                  # adapter -> running count
    delays = [0.0] * len(jobs)
    swaps = rejected = max_running = max_waiting = 0
    wait_time = 0.0
    min_usage_waiting = 1.0
    t_prev: Optional[float] = None
    t_first = jobs[order[0]].ts if jobs else 0.0
    t_last = t_first
    seq = 0

    def try_admit(now: float) -> None:
        nonlocal used, swaps, seq, max_running, running_seqs
        while waiting:
            j = jobs[waiting[0]]
            if running_seqs + j.seqs > max_num_seqs or used + j.tokens > kv_tokens:
                return
            a = j.adapter
            if a is not None and a not in slots:
                if max_loras <= 0:
                    return
                if len(slots) >= max_loras:
                    idle = [x for x in slots if in_use.get(x, 0) == 0]
                    if not idle:
                        return
                    victim = min(idle, key=lambda x: (slots[x], x))
                    del slots[victim]
                    swaps += 1
                slots[a] = now
            i = waiting.pop(0)
            if a is not None:
                slots[a] = now
                in_use[a] = in_use.get(a, 0) + 1
            used += j.tokens
            running_seqs += j.seqs
            delays[i] = now - j.ts
            heapq.heappush(running, (now + j.service_s, seq, i))
            seq += 1
            max_running = max(max_running, running_seqs)

    def advance(now: float) -> None:
        nonlocal wait_time, t_prev
        if t_prev is not None and waiting:
            wait_time += now - t_prev
        t_prev = now

    samples = sorted(sample_at)
    timeline: list = []
    si = 0

    def sample_until(t: float) -> None:
        nonlocal si
        while si < len(samples) and samples[si] < t:
            timeline.append((samples[si], running_seqs, len(waiting), used))
            si += 1

    k = 0
    while k < len(order) or running:
        next_arr = jobs[order[k]].ts if k < len(order) else math.inf
        next_dep = running[0][0] if running else math.inf
        sample_until(min(next_arr, next_dep))
        if next_dep <= next_arr:
            now = next_dep
            advance(now)
            while running and running[0][0] <= now:
                _, _, i = heapq.heappop(running)
                used -= jobs[i].tokens
                running_seqs -= jobs[i].seqs
                a = jobs[i].adapter
                if a is not None:
                    in_use[a] -= 1
                    slots[a] = now
            t_last = max(t_last, now)
        else:
            now = next_arr
            advance(now)
            i = order[k]
            k += 1
            if jobs[i].tokens > kv_tokens or jobs[i].seqs > max_num_seqs:
                rejected += 1
                continue
            waiting.append(i)
        try_admit(now)
        if waiting:
            max_waiting = max(max_waiting, len(waiting))
            min_usage_waiting = min(min_usage_waiting, used / kv_tokens if kv_tokens else 1.0)
        t_last = max(t_last, now)

    sample_until(math.inf)
    counted = sorted(delays[i] for i in range(len(jobs))
                     if jobs[i].counted and jobs[i].tokens <= kv_tokens)
    p95 = counted[min(len(counted) - 1, int(math.ceil(0.95 * len(counted))) - 1)] if counted else 0.0
    span = t_last - t_first
    return ReplayResult(
        n=len(jobs), total_queue_s=sum(counted), p95_queue_s=p95,
        max_running=max_running, max_waiting=max_waiting,
        frac_time_waiting=(wait_time / span) if span > 0 else 0.0,
        swaps=swaps, rejected=rejected, imputed=sum(1 for j in jobs if j.imputed),
        min_kv_usage_when_waiting=min_usage_waiting, delays=tuple(delays),
        timeline=tuple(timeline))


# --- windows and demand estimates -------------------------------------------

def past_windows(now: float, window_s: float, count: int,
                 stride_s: float = WEEK_S) -> List[Tuple[float, float]]:
    """`count` windows of `window_s` ending at `now`, `now − stride`, … .

    The default stride is a week so each window is the same hour-of-week: a
    composition is scored against several comparable past windows rather than
    one point forecast (design §6)."""
    return [(now - i * stride_s - window_s, now - i * stride_s) for i in range(count)]


def in_window(records: Iterable[Mapping], w: Tuple[float, float]) -> List[Mapping]:
    lo, hi = w
    return [r for r in records if lo <= float(r["ts"]) < hi]


def score_windows(score: Callable[[Sequence[Mapping]], float], records: Sequence[Mapping],
                  windows: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """(mean, worst) of `score` over the windows. With no windows the whole
    trace is one window."""
    vals = [score(in_window(records, w)) for w in windows] if windows else [score(records)]
    return sum(vals) / len(vals), max(vals)


def decayed_rate(records: Iterable[Mapping], now: float, half_life_s: float = HOUR_S) -> float:
    """Exponentially decayed arrival rate, requests/s at `now`.

    Each past request contributes λ·exp(−λ·age), λ = ln2 / half_life, which is
    an unbiased estimate of a constant rate over a long enough trace. Records
    after `now` are ignored (a replayed snapshot must not see its future)."""
    lam = math.log(2) / half_life_s
    return sum(lam * math.exp(-lam * (now - float(r["ts"])))
               for r in records if float(r["ts"]) <= now)


def same_hour_last_week_rate(records: Sequence[Mapping], now: float) -> Optional[float]:
    """Requests/s in [now − 7d, now − 7d + 1h). None when the trace does not
    reach back that far — "no data" is not "no demand"."""
    if not records or min(float(r["ts"]) for r in records) > now - WEEK_S:
        return None
    lo = now - WEEK_S
    return len(in_window(records, (lo, lo + HOUR_S))) / HOUR_S


def expected_rate(records: Sequence[Mapping], now: float) -> Tuple[float, str]:
    """The rate the change cost uses: the larger of the two estimates (a restart
    should be priced against the busier plausible hour). Returns the basis."""
    d = decayed_rate(records, now)
    w = same_hour_last_week_rate(records, now)
    if w is not None and w > d:
        return w, "same_hour_last_week"
    return d, "decayed_recent"
