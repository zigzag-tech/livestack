"""The composition loop end to end, on the facts harmony-llm serves.

The facts are what `/composition/facts` returns, built from the verbatim
2026-09-27 vLLM startup logs. The scenario is that day's: `llm_general` runs
chips-only with bf16 KV, jemm demand arrives, and the right answer is the
composition that was applied by hand (chips + jemm, fp8 KV)."""
import json
from pathlib import Path

from livestack_node import compose
from livestack_node.ledger import JsonlLedger, validate
from livestack_node.snapshots import SnapshotStore
from livestack_node.vllm_startup import composition_hash, key_from_launch, parse

FIX = Path(__file__).parent / "fixtures" / "vllm_startup"
BASE = "dbirks/Qwen3.8-27B-W4A16-AutoRound"
T0 = 1_790_000_000.0


def _row(fixture, adapters, kv, at):
    args = ["--max-model-len", "24576", "--max-num-seqs", "32"] + (["--kv-cache-dtype", kv] if kv != "auto" else [])
    key = key_from_launch(BASE, args, adapters, "0.28.0")
    h = composition_hash(key)
    m = parse((FIX / fixture).read_text().splitlines(), composition_hash=h, now=at)
    return {**m.to_json(), "unit": "llm_general", "host_id": "xc-tower-ubuntu",
            "composition": json.loads(key.canonical())}


BF16 = _row("v0.28.0-llm_general-bf16.log", {"chips": 16}, "auto", T0 - 86400)
FP8 = _row("v0.28.0-llm_general-fp8.log", {"chips": 16, "jemm": 16}, "fp8", T0 + 600)


def _trace(n_chips=60, n_jemm=100):
    recs = []
    for i in range(n_chips):
        recs.append({"ts": T0 - 3000 + i * 30, "unit": "llm_general", "composition_hash": BF16["composition_hash"],
                     "adapter": "chips", "owner_ns": "benchday:", "prompt_tokens": 2400,
                     "completion_tokens": 100, "elapsed_ms": 4000.0, "queue_ms": None, "outcome": "ok"})
    for i in range(n_jemm):
        recs.append({"ts": T0 - 3000 + 10 + i * 30, "unit": "llm_general", "composition_hash": None,
                     "adapter": "jemm", "owner_ns": "benchday:", "prompt_tokens": 900,
                     "completion_tokens": 1, "elapsed_ms": 800.0, "queue_ms": None, "outcome": "unsatisfied"})
    return recs


def facts(rows=(BF16,), trace=None, kv=("auto", "fp8")):
    return {"host_id": "xc-tower-ubuntu", "device_id": "xc-tower-ubuntu/gpu1",
            "capacity_bytes": int(23.56 * (1 << 30)), "kv_dtypes": list(kv),
            "units": [{"name": "llm_general", "model": BASE,
                       "adapters": {"chips": 16}, "adapter_paths": {"chips": "/var/lib/harmony/adapters/chips"},
                       "lora_base": "Qwen/Qwen3.8-27B",
                       "kv_dtype": "auto", "max_model_len": 24576, "max_num_seqs": 32, "gpu_fraction": 0.96,
                       "extra_args": "--max-num-seqs 32 --reasoning-parser qwen3", "residency": "UNPINNED",
                       "resident": True, "composition_hash": BF16["composition_hash"], "measured": BF16}],
            "adapter_catalogue": [
                {"name": "chips", "path": "/var/lib/harmony/adapters/chips", "rank": 16, "lora_base": "Qwen/Qwen3.8-27B"},
                {"name": "jemm", "path": "/var/lib/harmony/adapters/jemm", "rank": 16, "lora_base": "Qwen/Qwen3.8-27B"},
                {"name": "other-base", "path": "/x", "rank": 8, "lora_base": "Qwen/Qwen3-8B"}],
            "measured_rows": list(rows), "trace": _trace() if trace is None else trace, "now": T0}


def test_proposes_the_hand_applied_composition_and_records_it(tmp_path):
    led = JsonlLedger(str(tmp_path / "c.jsonl"))
    store = SnapshotStore(str(tmp_path / "snaps"))
    f = facts(rows=(BF16, FP8))
    out = compose.propose([f], ledger=led, store=store, now=T0, params={"lens": [24576], "seqs": [32]})
    chosen = out["decision"]["chosen"]
    assert chosen != "keep", out["decision"]["reason"]
    assert sorted(chosen["adapters"]) == ["chips", "jemm"] and chosen["kv_dtype"] == "fp8"
    diff = out["units_diff"]
    assert diff["unit"] == "llm_general"
    fields = {c["field"]: c for c in diff["changes"]}
    assert fields["adapters"]["to"] == {"chips": "/var/lib/harmony/adapters/chips",
                                        "jemm": "/var/lib/harmony/adapters/jemm"}
    assert fields["extra_args"]["to"].endswith("--kv-cache-dtype fp8")
    rows = led.read()
    assert len(rows) == 1 and validate(rows[0]) == []
    rec = rows[0]
    assert rec["emitter"] == "composition" and rec["decision"] == "compose"
    assert rec["snapshot"] == out["snapshot"]
    bf16_two = [c for c in rec["candidates"] if c["reason"] == "filtered:infeasible:kv_tokens<max_model_len"]
    assert bf16_two, "the bf16 two-adapter composition must be on record as infeasible"
    # Re-run from the stored facts: same answer.
    assert compose.replay(rec, store)["reproduced"] is True


def test_without_jemm_demand_it_keeps(tmp_path):
    out = compose.propose([facts(rows=(BF16, FP8), trace=_trace(n_jemm=0))], ledger=None, store=None,
                          now=T0, params={"lens": [24576], "seqs": [32]})
    assert out["decision"]["chosen"] == "keep"
    assert out["units_diff"] is None


def test_unknown_card_size_is_named_not_zero():
    f = facts(rows=())
    f["capacity_bytes"] = None
    f["units"][0]["measured"] = None
    f["units"][0]["gpu_fraction"] = 0.96
    state, notes = compose.state_from_facts([f], now=T0)
    assert state.devices == () and "unknown" in notes[0]


def test_outcomes_join_once_each(tmp_path):
    led = JsonlLedger(str(tmp_path / "c.jsonl"))
    f = facts(rows=(BF16, FP8))
    compose.propose([f], ledger=led, store=None, now=T0, params={"lens": [24576], "seqs": [32]})
    served = [{"ts": FP8["measured_at"] + 60 + i, "unit": "llm_general", "adapter": "jemm",
               "composition_hash": FP8["composition_hash"], "elapsed_ms": 700.0 + i, "outcome": "ok"}
              for i in range(10)]
    after = facts(rows=(BF16, FP8), trace=served)
    now = FP8["measured_at"] + 2.5 * 3600
    assert compose.join_outcomes(led, [after], now=now) == 1 + 2      # measured + hours 0,1
    assert compose.join_outcomes(led, [after], now=now) == 0          # idempotent
    rows = led.read()
    measured = next(r for r in rows if (r.get("outcome") or {}).get("kind") == "measured")
    assert measured["parent_decision_id"] == rows[0]["decision_id"]
    assert measured["outcome"]["measured"]["kv_tokens"] == 37981
    assert set(measured["outcome"]["error"]) >= {"weights_gib", "kv_tokens"}
    hour0 = next(r for r in rows if (r.get("outcome") or {}).get("hour") == 0)
    assert hour0["outcome"]["requests"] == 10 and hour0["outcome"]["ok"] == 10
    assert all(validate(r) == [] for r in rows)


def test_a_proposal_never_applied_is_marked_after_seven_days(tmp_path):
    led = JsonlLedger(str(tmp_path / "c.jsonl"))
    f = facts(rows=(BF16, FP8))
    compose.propose([f], ledger=led, store=None, now=T0, params={"lens": [24576], "seqs": [32]})
    never = facts(rows=(BF16,), trace=[])
    assert compose.join_outcomes(led, [never], now=T0 + 3 * 86400) == 0
    assert compose.join_outcomes(led, [never], now=T0 + 8 * 86400) == 1
    last = led.read()[-1]
    assert last["outcome"]["kind"] == "not_applied" and last["outcome"]["status"] == "unknown"


def test_card_size_comes_from_the_engine_not_nvidia_smi():
    """nvidia-smi says 24.0 GiB; vLLM budgets against the 23.56 GiB CUDA exposes.
    With nvidia-smi's number the bf16 two-adapter composition looked feasible on
    2026-09-28 (28,133 KV tokens predicted); vLLM refuses to start it."""
    f = facts(rows=(BF16, FP8))
    f["capacity_bytes"] = 24576 * (1 << 20)            # what nvidia-smi reports
    f["units"][0]["measured"] = FP8                     # the live engine's own report
    out = compose.propose([f], ledger=None, store=None, now=T0, params={"lens": [24576], "seqs": [32]})
    two_bf16 = [c for c in out["decision"]["candidates"]
                if sorted(c["composition"]["adapters"]) == ["chips", "jemm"]
                and c["composition"]["kv_dtype"] == "auto"]
    assert two_bf16 and all(c["reason"] == "filtered:infeasible:kv_tokens<max_model_len" for c in two_bf16)
    state, notes = compose.state_from_facts([f], now=T0)
    assert abs(state.devices[0].capacity / (1 << 30) - 23.56) < 0.02
    assert notes == []


def test_the_recorded_decision_keeps_the_live_row_under_the_size_cap(tmp_path):
    """The first production run's record exceeded the ledger's 32 KiB cap and
    the writer shed the live composition's row. With the full search space
    (3 lengths x 2 caps x 2 dtypes x 4 adapter sets) the record must fit."""
    led = JsonlLedger(str(tmp_path / "c.jsonl"))
    compose.propose([facts(rows=(BF16, FP8))], ledger=led, store=None, now=T0)
    rec = led.read()[0]
    assert not rec.get("truncated")
    assert any(c["detail"]["live"] for c in rec["candidates"])
    assert len(json.dumps(rec, separators=(",", ":"), sort_keys=True)) <= 32 * 1024   # as the ledger measures


def test_an_oversized_record_drops_rows_itself_never_the_live_one(tmp_path, monkeypatch):
    import livestack_node.ledger as L
    monkeypatch.setattr(L, "MAX_RECORD_BYTES", 12 * 1024)
    led = JsonlLedger(str(tmp_path / "c.jsonl"))
    compose.propose([facts(rows=(BF16, FP8))], ledger=led, store=None, now=T0)
    rec = led.read()[0]
    assert not rec.get("truncated")
    assert rec["request"]["omitted_for_size"] > 0
    assert any(c["detail"]["live"] for c in rec["candidates"])
