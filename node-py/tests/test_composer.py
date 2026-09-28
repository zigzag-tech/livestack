"""Deciders propose; `run_composition` judges every proposal and the live
composition with the same functions, and its record replays exactly."""
import json
from dataclasses import replace

import pytest

from livestack_node import composition as cm
from test_composition import (BASE, CHIPS, DEV, JEMM, WEIGHTS, calibration_state, comp,
                              trace)

W = cm.load_weights(WEIGHTS)


class Fixed:
    name, version = "fixed", "1"

    def __init__(self, props):
        self.props = props

    def propose(self, state):
        return list(self.props)


def jemm_demand_state():
    """2026-09-28: jemm requests arrive and the live chips-only unit cannot
    serve them. The decision should be the composition that was applied by hand.

    100 jemm requests: at 30 each unserved they outweigh the restart plus the
    two never-started flags (fp8 KV, a second LoRA slot: 2 × 600). With 40 they
    do not, and the run keeps — the weights, not the code, set that line.
    60 chips requests: with exactly 40, dropping chips for jemm alone (40 × 30
    unserved) ties the two new flags (2 × 600) to the unit."""
    return calibration_state(trace=trace(n=60) + trace(n=100, adapter=JEMM, start=1_000_010.0),
                             now=1_000_000.0 + 100 * 30.0)


def test_the_2026_09_28_decision_is_reproduced():
    # The operator required the 24,576 context; see
    # test_equal_cost_prefers_the_longer_context for when 16,384 is allowed.
    st = replace(jemm_demand_state(),
                 search=replace(jemm_demand_state().search, max_model_lens=(24576,)))
    d = cm.run_composition(st, cm.ExhaustiveComposer(), W)
    assert d.chosen == comp([CHIPS, JEMM], kv="fp8")
    by = {(r.composition.kv_dtype, frozenset(r.composition.adapters),
           r.composition.max_model_len, r.composition.max_num_seqs): r for r in d.candidates}
    bf16 = by[("auto", frozenset({CHIPS, JEMM}), 24576, 32)]
    assert bf16.outcome == "filtered"
    assert bf16.reason == "filtered:infeasible:kv_tokens<max_model_len"
    assert by[("auto", frozenset({CHIPS}), 24576, 16)].reason == \
        "unknown:no_measured_basis:max_num_seqs"
    assert d.policy == {"composer": "exhaustive", "composer_version": "1", "weights": W.hash}
    assert d.to_json()["emitter"] == "composition" and d.to_json()["decision"] == "compose"


def test_equal_cost_prefers_the_longer_context():
    # With 16,384 allowed, bf16 chips+jemm at 16k costs exactly what fp8 at 24k
    # costs on this light trace (two new flags each, neither queues). The tie
    # must not be broken by hash: equal evidence keeps the caller's window.
    d = cm.run_composition(jemm_demand_state(), cm.ExhaustiveComposer(), W)
    assert d.chosen is not None and d.chosen.adapters == frozenset({CHIPS, JEMM})
    assert d.chosen.max_model_len == 24576
    assert d.chosen == comp([CHIPS, JEMM], kv="fp8")


def test_an_infeasible_proposal_is_filtered_and_never_chosen():
    over = comp([CHIPS, JEMM])      # bf16: exceeds what the budget leaves for KV
    d = cm.run_composition(jemm_demand_state(), Fixed([over]), W)
    row = next(r for r in d.candidates if r.composition == over)
    assert row.outcome == "filtered" and row.reason.startswith("filtered:infeasible:")
    assert d.chosen is None and d.chosen_hash == "keep"
    assert d.filtered["filtered:infeasible:kv_tokens<max_model_len"] == 1


def test_hard_pin_is_filtered_by_name():
    other = "Qwen/Qwen3-8B"
    st = replace(jemm_demand_state(), hard_pins=frozenset({(DEV, BASE)}))
    d = cm.run_composition(st, Fixed([comp([], base=other)]), W)
    row = next(r for r in d.candidates if r.composition.base == other)
    assert row.reason == "filtered:hard_pin" and row.outcome == "filtered"


def test_decision_replays_identically():
    a = cm.run_composition(jemm_demand_state(), cm.ExhaustiveComposer(), W)
    b = cm.run_composition(jemm_demand_state(), cm.ExhaustiveComposer(), W)
    assert json.dumps(a.to_json(), sort_keys=True) == json.dumps(b.to_json(), sort_keys=True)

    class Reversed(cm.ExhaustiveComposer):
        def propose(self, state):
            return list(reversed(super().propose(state)))

    c = cm.run_composition(jemm_demand_state(), Reversed(), W)
    assert c.chosen_hash == a.chosen_hash
    assert sorted((r.hash, r.feasibility, r.reason) for r in c.candidates) == \
        sorted((r.hash, r.feasibility, r.reason) for r in a.candidates)


def test_ledger_cap_is_64_and_always_keeps_live():
    # Widen the space past 64: every length/cap pairing, three dtypes.
    st = replace(jemm_demand_state(),
                 search=cm.SearchSpace(("auto", "fp8", "fp8_e5m2"),
                                       (8192, 12288, 16384, 20480, 24576, 32768),
                                       (8, 16, 32, 64)))
    d = cm.run_composition(st, cm.ExhaustiveComposer(), W)
    assert d.candidates_total > 64
    assert len(d.candidates) == 64
    assert any(r.live for r in d.candidates)
    assert any(r.outcome == "chosen" for r in d.candidates)
    scored = [r.cost.total for r in d.candidates if r.cost is not None]
    assert scored == sorted(scored)
    assert sum(d.filtered.values()) >= d.candidates_total - 64


def test_exhaustive_composer_size_and_cap():
    st = calibration_state()
    props = cm.ExhaustiveComposer().propose(st)
    # 1 device × 2^2 adapter subsets × 2 dtypes × 2 lengths × 2 caps
    assert len(props) == 32 and len(set(props)) == 32
    with pytest.raises(cm.CompositionSpaceTooLarge):
        cm.ExhaustiveComposer(max_candidates=31).propose(st)
