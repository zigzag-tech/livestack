"""The validator reads vLLM's own stats lines and engine lifetimes correctly,
and scores agreement the way its report says. The real-data run is the CLI
(HARMONY.md, "Unit composition"); this pins the parsing and the arithmetic."""
from livestack_node.replay_validate import compare, parse_journal

LINES = [
    "100.0 h python[1]: [harmony-llm] vLLM ready: llm_general",
    "110.0 h python[1]: (APIServer pid=2) INFO 09-30 19:57:01 [loggers.py:310] Engine 000: Avg prompt "
    "throughput: 688.5 tokens/s, Avg generation throughput: 23.6 tokens/s, Running: 7 reqs, Waiting: 6 reqs, "
    "Deferred: 1 reqs, GPU KV cache usage: 84.8%, Prefix cache hit rate: 1.4%",
    "120.0 h python[1]: (APIServer pid=2) INFO x Engine 000: Running: 0 reqs, Waiting: 0 reqs, "
    "GPU KV cache usage: 0.0%, Prefix cache hit rate: 1.4%",
    "130.0 h python[1]: [harmony-llm] stopping vLLM: llm_general",
    "140.0 h python[1]: [harmony-llm] vLLM ready: llm_general",
    "150.0 h python[1]: [harmony-llm] vLLM ready: other_unit",
    "160.0 h python[1]: [harmony-llm] vLLM ready: llm_general",
]


def test_stats_lines_and_lifetimes():
    samples, segments = parse_journal(LINES, "llm_general")
    assert samples[0] == {"t": 110.0, "running": 7, "waiting": 6, "deferred": 1, "kv": 0.848}
    assert samples[1]["deferred"] is None and samples[1]["kv"] == 0.0
    # 140 -> 160 had no stop line (a systemctl restart kills the logger first).
    assert segments == [(100.0, 130.0), (140.0, 160.0), (160.0, float("inf"))]


def _rec(ts, n=None, tokens=1000):
    return {"ts": ts, "prompt_tokens": tokens - 100, "completion_tokens": 100,
            "elapsed_ms": 10_000.0, "n": n, "adapter": None, "outcome": "ok"}


def test_agreement_is_scored_per_sample():
    # Three 1000-token requests at t=0 into a 2000-token pool: two run, one waits.
    records = [_rec(0.0), _rec(0.0), _rec(0.0)]
    samples = [{"t": 1.0, "running": 2, "waiting": 1, "deferred": None, "kv": 1.0},
               {"t": 30.0, "running": 0, "waiting": 0, "deferred": None, "kv": 0.0}]
    r = compare(samples, records, kv_tokens=2000, max_num_seqs=32, max_loras=0)
    assert r["running"]["mae"] == 0 and r["waiting_any"]["tp"] == 1 and r["waiting_any"]["tn"] == 1
    assert r["actual_waiting_by_kv"] == {"100-110%": {"actual": 1, "model_also": 1}}
    assert r["records_with_n"] == 0


def test_waiting_the_model_cannot_explain_is_counted_as_a_miss():
    # Engine reports waiting at 40% KV; one small request cannot make the model wait.
    samples = [{"t": 1.0, "running": 1, "waiting": 3, "deferred": None, "kv": 0.4}]
    r = compare(samples, [_rec(0.0, n=1)], kv_tokens=10_000, max_num_seqs=32, max_loras=0)
    assert r["waiting_any"]["fn"] == 1 and r["waiting_any"]["recall"] == 0.0
    assert r["actual_waiting_by_kv"] == {"40-50%": {"actual": 1, "model_also": 0}}


def test_no_usable_records_is_an_error_not_a_zero():
    r = compare([{"t": 1.0, "running": 0, "waiting": 0, "deferred": None, "kv": 0.0}],
                [{"ts": 0.0, "prompt_tokens": None, "completion_tokens": None, "elapsed_ms": 5.0}],
                kv_tokens=1000, max_num_seqs=8, max_loras=0)
    assert "error" in r
