import json
import os
import time

from livestack_node.demand_log import (
    DemandLog, UnitCostStore, UsageTail, demand_log_from_env, owner_namespace, requirement_hash)


def _rows(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def test_unset_age_window_disables_and_says_why(tmp_path, monkeypatch):
    monkeypatch.delenv("HARMONY_DEMAND_LOG_AGE_DAYS", raising=False)
    monkeypatch.setenv("HARMONY_DEMAND_LOG_DIR", str(tmp_path))
    log = demand_log_from_env("llm")
    assert not log.enabled
    assert log.status()["reason"] == "disabled: no age window"
    log.record(unit="x")            # accepted and ignored; never raises
    assert not os.path.exists(tmp_path / "llm.jsonl")


def test_records_are_written_off_the_caller(tmp_path):
    log = DemandLog(str(tmp_path / "d.jsonl"), max_age_s=86400)
    log.record(unit="llm_general", adapter="jemm", prompt_tokens=None, outcome="ok")
    log.flush()
    rows = _rows(tmp_path / "d.jsonl")
    assert rows == [{"unit": "llm_general", "adapter": "jemm", "prompt_tokens": None, "outcome": "ok"}]
    assert log.status()["written"] == 1


def test_full_queue_drops_and_counts(tmp_path):
    log = DemandLog(str(tmp_path / "d.jsonl"), max_age_s=86400, queue_max=1)
    # Stall the drain: hold the ledger's lock so the writer cannot finish.
    with log._ledger._lock:
        for i in range(50):
            log.record(i=i)
        assert log.dropped > 0
    log.flush()
    assert log.status()["dropped"] == log.dropped


def test_size_bound_rotates(tmp_path):
    log = DemandLog(str(tmp_path / "d.jsonl"), max_age_s=86400, max_bytes=2048, max_files=2)
    for i in range(200):
        log.record(i=i, pad="x" * 50)
    log.flush()
    files = sorted(os.listdir(tmp_path))
    assert files == ["d.jsonl", "d.jsonl.1"]
    assert all(os.path.getsize(tmp_path / f) <= 2048 for f in files)


def test_owner_namespace_keeps_the_application_not_the_person():
    assert owner_namespace("benchday:acct_123") == "benchday:"
    assert owner_namespace("acct_123") is None
    assert owner_namespace(None) is None


def test_usage_tail_reads_tokens_or_none():
    t = UsageTail(limit=64)
    t.feed(b'{"choices":[{"text":"' + b"x" * 500 + b'"}],')
    t.feed(b'"usage":{"prompt_tokens":1423,"completion_tokens":1}}')
    assert t.tokens() == (1423, 1)
    empty = UsageTail()
    empty.feed(b"data: {\"choices\":[]}\n\n")
    assert empty.tokens() == (None, None)


def test_requirement_hash_is_stable():
    assert requirement_hash({"b": 1, "a": 2}) == requirement_hash({"a": 2, "b": 1})
    assert requirement_hash(None) is None


def test_cost_store_keeps_latest_per_hash_and_is_bounded(tmp_path):
    s = UnitCostStore(str(tmp_path / "c.jsonl"), max_rows=3)
    for i in range(5):
        s.put({"composition_hash": f"h{i}", "measured_at": i})
    s.put({"composition_hash": "h4", "measured_at": 9, "kv_tokens": 1})
    rows = s.load()
    assert set(rows) == {"h2", "h3", "h4"}
    assert rows["h4"]["kv_tokens"] == 1
