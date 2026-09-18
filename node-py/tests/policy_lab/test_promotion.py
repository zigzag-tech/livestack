from livestack_node.policy_lab.promotion import CellComparison, evaluate_promotion


def _cell(name="tts", candidate_good=True, candidate_latency=90, background=False):
    return CellComparison(
        workload_class=name,
        requester_region="canada",
        interactive=not background,
        incumbent_good=tuple([True] * 40),
        candidate_good=tuple([candidate_good] * 40),
        incumbent_first_output_us=tuple([100] * 40),
        candidate_first_output_us=tuple([candidate_latency] * 40),
        incumbent_background_throughput=tuple([10.0] * 40) if background else (),
        candidate_background_throughput=tuple([10.0] * 40) if background else (),
    )


def test_candidate_with_real_latency_improvement_passes_per_cell_guards():
    result = evaluate_promotion(
        (_cell(),),
        claimed_primary="first_output",
        calibrated=True,
        invariants_pass=True,
        uncertainty_invariants=(True, True),
    )
    assert result.status == "offline_qualified"


def test_aggregate_win_cannot_hide_regional_interactive_regression():
    good = _cell(name="llm", candidate_latency=50)
    bad = CellComparison(
        workload_class="dictation",
        requester_region="canada",
        interactive=True,
        incumbent_good=tuple([True] * 40),
        candidate_good=tuple([False] * 40),
        incumbent_first_output_us=tuple([100] * 40),
        candidate_first_output_us=tuple([200] * 40),
        incumbent_background_throughput=(),
        candidate_background_throughput=(),
    )
    result = evaluate_promotion(
        (good, bad), claimed_primary="first_output", calibrated=True,
        invariants_pass=True, uncertainty_invariants=(True,)
    )
    assert result.status == "regression"
    assert any("dictation/canada" in reason for reason in result.reasons)


def test_s26_reject_all_cannot_win_and_insufficient_samples_are_distinct():
    rejected = evaluate_promotion(
        (_cell(candidate_good=False, candidate_latency=1),),
        claimed_primary="first_output", calibrated=True,
        invariants_pass=True, uncertainty_invariants=(True,)
    )
    assert rejected.status == "regression"

    tiny = _cell()
    tiny = CellComparison(**{**tiny.__dict__, "incumbent_good": (True,) * 5, "candidate_good": (True,) * 5,
                             "incumbent_first_output_us": (100,) * 5, "candidate_first_output_us": (90,) * 5})
    insufficient = evaluate_promotion(
        (tiny,), claimed_primary="first_output", calibrated=True,
        invariants_pass=True, uncertainty_invariants=(True,)
    )
    assert insufficient.status == "insufficient_evidence"


def test_uncertainty_invariant_failure_blocks_promotion():
    result = evaluate_promotion(
        (_cell(),), claimed_primary="first_output", calibrated=True,
        invariants_pass=True, uncertainty_invariants=(True, False)
    )
    assert result.status == "regression"
