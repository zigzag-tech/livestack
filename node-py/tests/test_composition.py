"""Feasibility against the 2026-09-28 recomposition of `llm_general`.

The measured rows are the verbatim vLLM 0.28.0 startup logs in
`fixtures/vllm_startup/`, parsed by the production parser — the calibration
is against what the engine said, not against numbers retyped here."""
import pytest
from dataclasses import replace
from pathlib import Path

from livestack_node import composition as cm
from livestack_node.vllm_startup import GIB, CompositionKey, MeasuredCost, parse

FIX = Path(__file__).parent / "fixtures" / "vllm_startup"
WEIGHTS = Path(cm.__file__).parent / "composition_weights" / "v1.json"
BASE = "dbirks/Qwen3.8-27B-W4A16-AutoRound"
DEV = "xc-tower-ubuntu:gpu1"
ENGINE = "0.28.0"
CHIPS, JEMM = "chips-settinghead-v1", "jemm"


def measured(name: str) -> MeasuredCost:
    m = parse((FIX / f"v0.28.0-llm_general-{name}.log").read_text().splitlines())
    assert isinstance(m, MeasuredCost)
    return m


def comp(adapters, kv="auto", ln=24576, seqs=32, base=BASE, dev=DEV):
    return cm.Composition(dev, base, frozenset(adapters), kv, ln, seqs)


def trace(n=40, tokens=2500, start=1_000_000.0, gap=30.0, adapter=CHIPS, owner="benchday:"):
    return tuple({"ts": start + i * gap, "unit": "llm_general", "composition_hash": "",
                  "adapter": adapter, "owner_ns": owner, "prompt_tokens": tokens - 100,
                  "completion_tokens": 100, "elapsed_ms": 4000.0, "queue_ms": None,
                  "outcome": "ok"} for i in range(n))


def calibration_state(**over) -> cm.CompositionState:
    adapters = {CHIPS: cm.Adapter(CHIPS, BASE, 16), JEMM: cm.Adapter(JEMM, BASE, 16)}
    rows = [
        (comp([CHIPS]).key(adapters, ENGINE), measured("bf16")),
        (comp([CHIPS, JEMM], kv="fp8").key(adapters, ENGINE), measured("fp8")),
    ]
    costs, keys = cm.measured_rows(rows)
    st = cm.CompositionState(
        devices=(cm.DeviceSpec(DEV, int(23.56 * GIB), 0.96),),
        adapters=adapters, bases=(BASE,), measured=costs, measured_keys=keys,
        trace=trace(), live={DEV: comp([CHIPS])},
        engine={DEV: cm.EngineFacts(ENGINE, frozenset({"auto", "fp8"}),
                                    frozenset({"--max-model-len=24576", "--max-num-seqs=32",
                                               "--max-loras=1"}))},
        search=cm.SearchSpace(("auto", "fp8"), (16384, 24576), (16, 32)),
        unit_bases={"llm_general": BASE}, now=1_000_000.0 + 40 * 30.0)
    return replace(st, **over)


def test_chips_only_bf16_predicts_the_measured_row_exactly():
    st = calibration_state()
    f = cm.feasible(st, comp([CHIPS]))
    assert isinstance(f, cm.Feasible)
    p, m = f.prediction, measured("bf16")
    assert not p.estimated
    assert (p.weights, p.activation, p.cuda_graphs, p.kv_bytes, p.kv_tokens) == \
        (m.weights_nontorch, m.peak_activation, m.cuda_graphs, m.kv_bytes, m.kv_tokens)


def test_chips_plus_jemm_bf16_is_infeasible_kv_tokens_below_context():
    f = cm.feasible(calibration_state(), comp([CHIPS, JEMM]))
    assert isinstance(f, cm.Infeasible)
    assert f.reason == "kv_tokens<max_model_len"
    p = f.prediction
    assert p.estimated
    # design §5: "about 22k tokens, below 24,576" — before the margin
    assert 21_500 < p.kv_tokens < 23_000, p.kv_tokens
    assert p.kv_tokens_checked < p.kv_tokens < 24576
    # weights/activation come from the fp8 row: the dtype assumption is on record
    assert "kv_dtype_independent" in p.assumptions


def test_chips_plus_jemm_fp8_is_feasible_within_margin_of_the_measured_row():
    f = cm.feasible(calibration_state(), comp([CHIPS, JEMM], kv="fp8"))
    assert isinstance(f, cm.Feasible)
    p, m = f.prediction, measured("fp8")
    for pred, meas in ((p.weights, m.weights_nontorch), (p.activation, m.peak_activation),
                       (p.cuda_graphs, m.cuda_graphs), (p.kv_tokens, m.kv_tokens)):
        assert abs(pred - meas) <= 0.10 * meas
    assert p.kv_tokens >= 24576


def test_additive_prediction_uses_the_delta_between_the_two_rows():
    # Three adapters: +1 adapter over the fp8 row. The per-adapter delta comes
    # from the bf16/1 -> fp8/2 pair (0.21 + 0.33 + 0.44 GiB, the "~1 GiB" the
    # second adapter really cost), and the graphs no longer fit outside the
    # 0.96 budget on a 23.56 GiB card.
    third = cm.Adapter("third", BASE, 16)
    st = calibration_state(adapters={**calibration_state().adapters, "third": third})
    f = cm.feasible(st, comp([CHIPS, JEMM, "third"], kv="fp8"))
    assert isinstance(f, cm.Infeasible) and f.reason == "cuda_graphs>outside_budget"
    assert "kv_dtype_independent" in f.prediction.assumptions
    assert abs(f.prediction.cuda_graphs / GIB - 1.34) < 0.01


def test_chips_only_fp8_is_estimated_from_the_fp8_tokens_per_byte():
    f = cm.feasible(calibration_state(), comp([CHIPS], kv="fp8"))
    assert isinstance(f, cm.Feasible) and f.prediction.estimated
    # 2.21 GiB of KV at the fp8 row's 37,981 / 1.66 GiB
    assert abs(f.prediction.kv_tokens - 2.21 * 37981 / 1.66) < 300


def test_never_measured_base_is_unknown_weights():
    other = "Qwen/Qwen3-8B"
    st = calibration_state(bases=(BASE, other))
    f = cm.feasible(st, comp([], base=other))
    assert isinstance(f, cm.Unknown) and f.reason == "no_measured_basis:weights"


def test_unmeasured_batch_cap_is_unknown_not_feasible():
    f = cm.feasible(calibration_state(), comp([CHIPS], seqs=16))
    assert isinstance(f, cm.Unknown) and f.reason == "no_measured_basis:max_num_seqs"


def test_lora_off_is_not_extrapolated_from_lora_on_rows():
    f = cm.feasible(calibration_state(), comp([]))
    assert isinstance(f, cm.Unknown) and f.reason.startswith("no_measured_basis:adapter_slot")


def test_hard_pin_filters_a_changed_base():
    st = calibration_state(hard_pins=frozenset({(DEV, BASE)}),
                           bases=(BASE, "Qwen/Qwen3-8B"))
    f = cm.feasible(st, comp([], base="Qwen/Qwen3-8B"))
    assert isinstance(f, cm.Infeasible) and f.reason == "hard_pin"
    assert isinstance(cm.feasible(st, comp([CHIPS])), cm.Feasible)


def test_the_other_hard_rules():
    st = calibration_state()
    assert cm.feasible(st, comp(["nope"])).reason == "unknown_adapter:nope"
    st2 = calibration_state(adapters={**st.adapters, "x": cm.Adapter("x", "other", 16)})
    assert cm.feasible(st2, comp([CHIPS, "x"])).reason == "adapter_base:x"
    st3 = calibration_state(engine={DEV: cm.EngineFacts(ENGINE, frozenset({"auto"}))})
    assert cm.feasible(st3, comp([CHIPS, JEMM], kv="fp8")).reason == "kv_dtype_unavailable:fp8"
    long = trace(n=3, tokens=30_000)
    st4 = calibration_state(trace=trace() + long)
    assert cm.feasible(st4, comp([CHIPS])).reason == "truncates:3"


def test_no_weight_changes_any_feasibility_result():
    """Property: perturb every cost weight (the safety margin is not a cost
    weight — it is fixed here) and the feasibility of every candidate the
    exhaustive composer proposes is unchanged."""
    st = calibration_state()
    base = cm.load_weights(WEIGHTS)

    def verdicts(w):
        d = cm.run_composition(st, cm.ExhaustiveComposer(), w)
        return sorted((r.device, r.hash, r.feasibility,
                       r.reason if r.feasibility != "unknown" or "cost" not in r.reason else "")
                      for r in d.candidates)

    ref = verdicts(base)
    assert len(ref) > 10
    cost_fields = ("queue_s", "unserved", "swap_stall_s", "restart_downtime_s",
                   "risk_prior_per_new_flag")
    for fld in cost_fields:
        for v in (0.0, 1e-6, 7.0, 1e6):
            assert verdicts(replace(base, **{fld: v})) == ref, (fld, v)


def test_weights_artifact_loads_with_a_content_hash():
    w = cm.load_weights(WEIGHTS)
    assert w.version == 1 and w.memory_margin == 0.10 and w.restart_downtime_s == 145
    assert w.hash.startswith("sha256:") and len(w.hash) == 71



def test_queueing_without_paging_facts_is_labelled_tokens():
    w = cm.load_weights(WEIGHTS)
    st = calibration_state(trace=trace(n=20))
    c = cm.cost(st, dict(st.live), w)
    assert c.kv_accounting and all(x.endswith(":tokens,service:blended") for x in c.kv_accounting)
    # Give every measured row paging facts and fitted rates: the label says so.
    paged = {h: MeasuredCost(**{**m.__dict__, "block_size": 1568, "state_pages_per_seq": 1.8,
                                "prefill_tok_s": 711.0, "decode_tok_s": 24.0})
             for h, m in st.measured.items()}
    st2 = replace(st, measured=paged)
    c2 = cm.cost(st2, dict(st2.live), w)
    assert all("pages:1568x1.8" in x and "service:711/24.0" in x for x in c2.kv_accounting)


def test_a_paged_base_with_an_unmeasured_dtype_block_is_unknown_not_cheap():
    """2026-09-30 dry run: fp8 rows carried block 1568, the bf16 row carried no
    block, and the bf16 candidate was priced with token accounting, queue-free,
    against a paged live composition. It must be unknown instead."""
    w = cm.load_weights(WEIGHTS)
    st = calibration_state(trace=trace(n=20))
    paged = {}
    for h, m in st.measured.items():
        k = st.measured_keys[h]
        extra = {"state_pages_per_seq": 1.8, "prefill_tok_s": 711.0, "decode_tok_s": 24.0}
        if k.kv_dtype == "fp8":
            extra["block_size"] = 1568                  # only fp8 measured its block
        paged[h] = MeasuredCost(**{**m.__dict__, **extra})
    st2 = replace(st, measured=paged)
    bf16 = next(c for c in st2.live.values())
    bf16 = replace(bf16, kv_dtype="auto")
    with pytest.raises(cm.CostUnknown, match="kv_block:auto"):
        cm.cost(st2, {bf16.device: bf16}, w)
