"""The replay admission model: exact on constructed traces, and checked for the
structural claims it can make against the live engine's journal.

The true end-to-end validation (replay 7 days of per-request demand, compare
with the journal's 10-second Running/Waiting samples) needs the demand log,
which does not exist yet — that test is SKIPPED by name, not absent."""
import json
import random
from pathlib import Path

import pytest

from livestack_node import composition_replay as rp

JOURNAL = Path(__file__).parent / "fixtures" / "composition" / \
    "journal-llm_general-2026-09-28.json"


def rec(ts, tokens, elapsed_ms=None, adapter=None, owner="benchday:", prompt=None):
    return {"ts": ts, "unit": "llm_general", "adapter": adapter, "owner_ns": owner,
            "prompt_tokens": tokens if prompt is None else prompt,
            "completion_tokens": 0 if tokens is not None else None,
            "elapsed_ms": elapsed_ms, "queue_ms": None, "outcome": "ok"}


def test_exact_delay_when_the_pool_holds_one_request():
    rate = rp.fit_rate([rec(0, 60, 10_000)])
    assert rate == pytest.approx(10 / 60)
    jobs = rp.jobs_from_records([rec(0.0, 60), rec(1.0, 60)], rate)
    r = rp.replay(jobs, kv_tokens=100, max_num_seqs=8, max_loras=0)
    assert r.delays == pytest.approx((0.0, 9.0))
    assert r.total_queue_s == pytest.approx(9.0) and r.p95_queue_s == pytest.approx(9.0)
    assert r.max_running == 1 and r.max_waiting == 1
    assert r.frac_time_waiting == pytest.approx(9 / 20)
    assert r.min_kv_usage_when_waiting == pytest.approx(0.6)


def test_batch_cap_queues_even_with_kv_free():
    jobs = [rp.Job(ts=0.0, tokens=10, service_s=5.0) for _ in range(3)]
    r = rp.replay(jobs, kv_tokens=10_000, max_num_seqs=2, max_loras=0)
    assert r.max_running == 2
    assert r.total_queue_s == pytest.approx(5.0)
    assert r.min_kv_usage_when_waiting == pytest.approx(20 / 10_000)


def test_adapter_slots_force_swaps_when_demand_alternates():
    seq = ["a", "b", "a", "b"]
    jobs = [rp.Job(ts=float(i * 10), tokens=10, service_s=1.0, adapter=a)
            for i, a in enumerate(seq)]
    assert rp.replay(jobs, kv_tokens=1000, max_num_seqs=8, max_loras=1).swaps == 3
    assert rp.replay(jobs, kv_tokens=1000, max_num_seqs=8, max_loras=2).swaps == 0


def test_busy_slot_blocks_a_different_adapter():
    jobs = [rp.Job(0.0, 10, 10.0, adapter="a"), rp.Job(1.0, 10, 1.0, adapter="b")]
    r = rp.replay(jobs, kv_tokens=1000, max_num_seqs=8, max_loras=1)
    assert r.delays == pytest.approx((0.0, 9.0)) and r.swaps == 1


def test_self_traffic_occupies_the_pool_but_is_not_counted():
    jobs = [rp.Job(0.0, 60, 10.0, counted=False), rp.Job(1.0, 60, 10.0)]
    r = rp.replay(jobs, kv_tokens=100, max_num_seqs=8, max_loras=0)
    assert r.total_queue_s == pytest.approx(9.0)
    jobs = [rp.Job(0.0, 60, 10.0), rp.Job(1.0, 60, 10.0, counted=False)]
    assert rp.replay(jobs, kv_tokens=100, max_num_seqs=8, max_loras=0).total_queue_s == 0.0


def test_unknown_tokens_are_imputed_and_counted_oversize_is_rejected():
    recs = [rec(0.0, 100), rec(1.0, 300), rec(2.0, None)]
    jobs = rp.jobs_from_records(recs, 0.01)
    assert [j.tokens for j in jobs] == [100, 300, 300] and jobs[2].imputed
    r = rp.replay(jobs + [rp.Job(3.0, 10_000, 1.0)], kv_tokens=1000, max_num_seqs=8,
                  max_loras=0)
    assert r.imputed == 1 and r.rejected == 1
    assert rp.fit_rate([rec(0, None, 100)]) is None


def test_demand_estimates_and_windows():
    now = 10 * rp.WEEK_S
    steady = [rec(now - i * 60.0, 10) for i in range(1, 60 * 24 * 8)]   # 1/min for 8 days
    assert rp.decayed_rate(steady, now) == pytest.approx(1 / 60, rel=0.05)
    assert rp.same_hour_last_week_rate(steady, now) == pytest.approx(1 / 60, rel=0.05)
    short = [r for r in steady if r["ts"] > now - 86_400]
    assert rp.same_hour_last_week_rate(short, now) is None        # no data is not zero
    assert rp.expected_rate(short, now)[1] == "decayed_recent"
    ws = rp.past_windows(now, 3600, 3)
    assert ws[0] == (now - 3600, now) and ws[2] == (now - 2 * rp.WEEK_S - 3600,
                                                    now - 2 * rp.WEEK_S)
    mean, worst = rp.score_windows(len, steady, ws)
    assert worst >= mean > 0


def _journal():
    return json.loads(JOURNAL.read_text())


def _saturating_trace(seed=7, n=3000):
    """Shaped like the live bf16 window: ~2.5k-token classifier requests (1.4k
    median prompt plus reasoning), arriving in bursts that fill the pool."""
    rnd = random.Random(seed)
    t, out = 0.0, []
    for _ in range(n):
        t += rnd.expovariate(1 / 0.6)
        out.append(rp.Job(ts=t, tokens=rnd.randint(2480, 2670), service_s=rnd.uniform(4, 9)))
    return out


def test_structural_max_running_is_set_by_the_kv_pool():
    live = next(w for w in _journal()["windows"] if w["kv_tokens"] == 29749)
    r = rp.replay(_saturating_trace(), kv_tokens=live["kv_tokens"], max_num_seqs=32,
                  max_loras=1)
    # The journal's ceiling is 11 running on 29,749 tokens; with ~2.5k-token
    # requests the model's ceiling is the pool divided by request size, and it
    # is the pool (not the batch cap of 32) that binds.
    assert r.max_running == live["max_running"] == 11
    assert r.frac_time_waiting > 0


def test_structural_model_queues_only_when_the_pool_is_nearly_full():
    r = rp.replay(_saturating_trace(), kv_tokens=29749, max_num_seqs=32, max_loras=1)
    assert r.max_waiting > 0
    assert r.min_kv_usage_when_waiting >= 0.91


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN GAP: the journal shows vLLM waiting at every KV decile (see fixture "
    "kv_usage_decile_hist_when_waiting; mode 80-90%), because vLLM admits on prompt "
    "blocks, defers, and budgets prefill tokens. This model reserves prompt+completion "
    "at admission and cannot wait below ~91%. Strict: if it ever passes, the model "
    "changed and this marker must go."))
def test_model_reproduces_waiting_below_90pct_kv_seen_in_journal():
    live = next(w for w in _journal()["windows"] if w["kv_tokens"] == 29749)
    below = sum(v for k, v in live["kv_usage_decile_hist_when_waiting"].items() if int(k) < 90)
    assert below > 0
    r = rp.replay(_saturating_trace(), kv_tokens=29749, max_num_seqs=32, max_loras=1)
    assert r.min_kv_usage_when_waiting < 0.90


@pytest.mark.skip(reason=(
    "MISSING INPUT: per-request demand log (openspec harmony-placement-foundation "
    "task 3.x). Replaying 7 days of the live composition and matching the journal's "
    "10-second Running/Waiting samples needs per-request ts/tokens/elapsed; the "
    "journal fixture alone has only the aggregate shape."))
def test_replay_matches_journal_end_to_end():
    raise AssertionError("unreachable until the demand log exists")


def test_an_n_sample_request_takes_n_batch_slots():
    """vLLM runs n samples as n sequences against max_num_seqs. Two n=12 jobs
    cannot run together under a cap of 16; two single jobs can."""
    wide = [rp.Job(ts=0.0, tokens=100, service_s=10.0, seqs=12),
            rp.Job(ts=0.0, tokens=100, service_s=10.0, seqs=12)]
    r = rp.replay(wide, kv_tokens=10_000, max_num_seqs=16, max_loras=0)
    assert r.max_running == 12 and r.total_queue_s == 10.0
    narrow = [rp.Job(ts=0.0, tokens=100, service_s=10.0) for _ in range(2)]
    r = rp.replay(narrow, kv_tokens=10_000, max_num_seqs=16, max_loras=0)
    assert r.max_running == 2 and r.total_queue_s == 0.0


def test_jobs_from_records_carry_n():
    recs = [{"ts": 0.0, "prompt_tokens": 90, "completion_tokens": 10, "elapsed_ms": 100.0, "n": 12},
            {"ts": 1.0, "prompt_tokens": 90, "completion_tokens": 10, "elapsed_ms": 100.0}]
    jobs = rp.jobs_from_records(recs, rate_s_per_token=0.001)
    assert [j.seqs for j in jobs] == [12, 1]
