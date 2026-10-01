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


def _validation_window():
    import gzip
    p = Path(__file__).parent / "fixtures" / "composition" / "replay-llm_general-2026-09-30.json.gz"
    with gzip.open(p, "rt") as fh:
        fx = json.load(fh)
    t0 = fx["t0"]
    samples = [{"t": t0 + t, "running": r, "waiting": w, "kv": kv} for t, r, w, kv in fx["samples"]]
    names = {0: None, 1: "chips-settinghead-v1", 2: "jemm"}
    records = [{"ts": t0 + t, "prompt_tokens": p, "completion_tokens": c, "elapsed_ms": e,
                "adapter": names[a], "n": n, "outcome": "ok"} for t, p, c, e, a, n in fx["records"]]
    return samples, records


def test_replay_matches_the_engine_on_a_real_lifetime():
    """The model against 4.6 h of vLLM's own stats lines (1,455 samples, 6,254
    requests; see _plans/composition-replay-validation.md). Tolerances are the
    measured agreement of the paged, two-rate model, rounded down: a change
    that makes the model worse fails here."""
    from livestack_node.replay_validate import compare
    samples, records = _validation_window()
    new = compare(samples, records, kv_tokens=37981, max_num_seqs=32, max_loras=2,
                  block_size=1568, state_pages=1.791, prefill_tok_s=711.3, decode_tok_s=23.93)
    run, kv, w = new["running"], new["kv_usage"], new["waiting_any"]
    assert abs(run["mean_model"] - run["mean_actual"]) <= 0.15 * run["mean_actual"]
    assert abs(kv["mean_model"] - kv["mean_actual"]) <= 0.10 * kv["mean_actual"]
    assert run["max_model"] <= 8                    # the pool holds ~8 short sequences
    assert w["recall"] >= 0.30 and w["precision"] >= 0.45


def test_negative_control_the_token_model_misses_all_queueing():
    """The pre-2026-09-30 model on the same window: it charges ~750 tokens a
    request where the engine holds ~3 pages of 1,568, so its pool never fills
    and it never predicts a single waiting sample."""
    from livestack_node.replay_validate import compare
    samples, records = _validation_window()
    old = compare(samples, records, kv_tokens=37981, max_num_seqs=32, max_loras=2)
    assert old["waiting_any"]["tp"] == 0
    assert old["kv_usage"]["mean_model"] < 0.25 * old["kv_usage"]["mean_actual"]


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


def test_timeline_samples_the_state_before_each_sample_time():
    jobs = [rp.Job(ts=0.0, tokens=600, service_s=10.0),
            rp.Job(ts=1.0, tokens=600, service_s=10.0)]          # waits: pool is 1000
    r = rp.replay(jobs, kv_tokens=1000, max_num_seqs=8, max_loras=0, sample_at=[0.5, 5.0, 10.5, 25.0])
    assert r.timeline == ((0.5, 1, 0, 600), (5.0, 1, 1, 600), (10.5, 1, 0, 600), (25.0, 0, 0, 0))


def test_paged_need_is_whole_pages_plus_per_sequence_state():
    j = rp.Job(ts=0.0, tokens=750, service_s=1.0, prompt=700, completion=50)
    assert rp.kv_need(j, 0, 0.0) == 750                         # token accounting
    assert rp.kv_need(j, 1568, 1.8) == 3 * 1568                 # ceil(1 + 1.8) pages
    big = rp.Job(ts=0.0, tokens=2000, service_s=1.0, prompt=1900, completion=100)
    assert rp.kv_need(big, 1568, 1.8) == 4 * 1568               # ceil(2 + 1.8)


def test_n_samples_become_n_jobs_and_only_the_first_pays_the_prompt():
    recs = [{"ts": 5.0, "prompt_tokens": 711, "completion_tokens": 240, "elapsed_ms": 1.0, "n": 12}]
    jobs = rp.jobs_from_records(recs, 1.0, prefill_tok_s=711.0, decode_tok_s=20.0)
    assert len(jobs) == 12 and all(j.seqs == 1 and j.ts == 5.0 for j in jobs)
    assert [j.prefill_tokens for j in jobs] == [711] + [0] * 11
    assert jobs[0].decode_s == 1.0                              # 20 tokens / 20 tok/s


def test_siblings_wait_for_the_shared_prompt_and_prefill_is_one_server():
    a = rp.Job(ts=0.0, tokens=100, service_s=0.0, prefill_tokens=1000, decode_s=1.0)
    sib = rp.Job(ts=0.0, tokens=100, service_s=0.0, prefill_tokens=0, decode_s=1.0)
    other = rp.Job(ts=0.0, tokens=100, service_s=0.0, prefill_tokens=1000, decode_s=1.0)
    r = rp.replay([a, sib, other], kv_tokens=10_000, max_num_seqs=8, max_loras=0,
                  prefill_tok_s=1000.0, sample_at=[1.5, 2.5, 3.5])
    # a: prefill 0-1, decode 1-2. sib: waits for a's prompt, decodes 1-2.
    # other: prefill queued behind a, 1-2, decodes 2-3.
    assert [run for _, run, _, _ in r.timeline] == [3, 1, 0]


def test_two_rate_fit_recovers_known_rates():
    recs = [{"prompt_tokens": p, "completion_tokens": c, "n": 1,
             "elapsed_ms": 1000.0 * (p / 700.0 + c / 25.0)}
            for p in range(100, 3000, 97) for c in (5, 20, 60)]
    pre, dec = rp.fit_two_rate(recs)
    assert abs(pre - 700.0) < 1.0 and abs(dec - 25.0) < 0.1
    assert rp.fit_two_rate(recs[:10]) is None                   # too few to fit
