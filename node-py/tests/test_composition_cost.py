"""Each cost term moves the right way on a constructed trace, and a marginal
gain never pays for a restart."""
from dataclasses import replace

import pytest

from livestack_node import composition as cm
from test_composition import (CHIPS, DEV, JEMM, WEIGHTS, calibration_state, comp, trace)

W = cm.load_weights(WEIGHTS)


def test_queue_grows_with_denser_demand_and_shrinks_with_a_bigger_pool():
    live = {DEV: comp([CHIPS])}
    sparse = cm.cost(calibration_state(), live, W)
    dense_st = calibration_state(trace=trace(n=200, gap=0.2, tokens=2500))
    dense = cm.cost(dense_st, live, W)
    assert sparse.queue_s == 0.0 and dense.queue_s > 0.0
    assert dense.operating == pytest.approx(W.queue_s * dense.queue_s)
    # fp8 KV on the same adapters: ~50k tokens instead of 29,749
    bigger = cm.cost(dense_st, {DEV: comp([CHIPS], kv="fp8")}, W)
    assert bigger.queue_s < dense.queue_s


def test_unserved_counts_demand_for_an_adapter_the_host_does_not_carry():
    st = calibration_state(trace=trace() + trace(n=10, adapter=JEMM, start=1_000_005.0))
    without = cm.cost(st, {DEV: comp([CHIPS])}, W)
    with_jemm = cm.cost(st, {DEV: comp([CHIPS, JEMM], kv="fp8")}, W)
    assert without.unserved == 10 and with_jemm.unserved == 0
    assert without.operating - with_jemm.operating == pytest.approx(
        10 * W.unserved + W.queue_s * (without.queue_s - with_jemm.queue_s))


def test_self_traffic_is_served_but_never_a_cost():
    st = calibration_state(trace=trace() + trace(n=10, adapter=JEMM, owner="jingway:",
                                                 start=1_000_005.0))
    c = cm.cost(st, {DEV: comp([CHIPS])}, W)
    assert c.unserved == 0 and c.self_traffic == 10


def test_swap_term_is_zero_while_every_adapter_has_a_slot():
    # harmony-llm launches max_loras = len(adapters), so alternating demand
    # cannot force a swap today; the mechanics are covered in the replay tests.
    alt = tuple({**r, "adapter": JEMM if i % 2 else CHIPS} for i, r in enumerate(trace()))
    c = cm.cost(calibration_state(trace=alt), {DEV: comp([CHIPS, JEMM], kv="fp8")}, W)
    assert c.swap_stalls == 0


def test_change_cost_is_requests_lost_to_the_restart_plus_new_flag_risk():
    st = calibration_state()
    live = cm.cost(st, dict(st.live), W)
    assert live.change_cost == 0 and live.new_flags == ()
    fp8 = cm.cost(st, {DEV: comp([CHIPS, JEMM], kv="fp8")}, W)
    assert sorted(f.removeprefix(DEV + ":") for f in fp8.new_flags) == \
        ["--kv-cache-dtype=fp8", "--max-loras=2"]
    assert fp8.rate > 0
    assert fp8.change_cost == pytest.approx(
        W.restart_downtime_s * fp8.rate * W.unserved + 2 * W.risk_prior_per_new_flag)
    # more demand in the apply window -> a dearer restart
    busy = cm.cost(calibration_state(trace=trace(n=400, gap=3.0),
                                     now=1_000_000.0 + 400 * 3.0),
                   {DEV: comp([CHIPS, JEMM], kv="fp8")}, W)
    assert busy.change_cost > fp8.change_cost
    # a host that has started fp8 before pays no risk prior for it
    facts = replace(st.engine[DEV], flags_started=st.engine[DEV].flags_started
                    | {"--kv-cache-dtype=fp8", "--max-loras=2"})
    seasoned = cm.cost(calibration_state(engine={DEV: facts}),
                       {DEV: comp([CHIPS, JEMM], kv="fp8")}, W)
    assert seasoned.change_cost == pytest.approx(fp8.change_cost - 2 * W.risk_prior_per_new_flag)


def test_windows_report_mean_and_worst():
    t = trace(n=100, gap=0.1, tokens=2500) + trace(n=10, start=1_100_000.0)
    st = calibration_state(trace=t, windows=((999_999.0, 1_000_060.0),
                                             (1_099_999.0, 1_100_400.0)))
    c = cm.cost(st, {DEV: comp([CHIPS])}, W)
    assert c.windows == 2
    assert c.queue_s_worst > c.queue_s > 0
    assert c.total_worst > c.total


def test_marginal_gain_below_change_cost_keeps_live():
    # A bigger pool helps a little on a lightly loaded trace; the restart and
    # the never-started fp8 flag cost far more.
    st = calibration_state(trace=trace(n=40, gap=2.0))

    class One:
        name, version = "one", "1"

        def propose(self, state):
            return [comp([CHIPS], kv="fp8")]

    d = cm.run_composition(st, One(), W)
    assert d.chosen is None and d.chosen_hash == "keep" and d.reason.startswith("keep:")
    cand = next(r for r in d.candidates if not r.live)
    live = next(r for r in d.candidates if r.live)
    assert cand.outcome == "ranked" and live.outcome == "chosen"
    assert cand.cost.operating <= live.cost.operating
    assert live.cost.operating - cand.cost.operating < cand.cost.change_cost
