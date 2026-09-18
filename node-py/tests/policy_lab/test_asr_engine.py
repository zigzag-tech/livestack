import pytest

from livestack_node.policy_lab.asr_engine import ASRError, ASRStream


def test_s09_live_audio_cannot_process_future_and_backlog_is_visible():
    stream = ASRStream("dictation", replay_window_us=200_000, max_buffer_us=2_000_000)
    stream.capture(0, 1_000_000)
    stream.process_through(400_000, at_us=600_000)
    assert stream.accepted_coverage_us == 1_000_000
    assert stream.processed_coverage_us == 400_000
    assert stream.backlog_us == 600_000
    with pytest.raises(ASRError, match="uncaptured future"):
        stream.process_through(1_100_000, at_us=700_000)


def test_partials_and_finalization_preserve_accepted_coverage():
    stream = ASRStream("dictation", replay_window_us=200_000, max_buffer_us=2_000_000)
    stream.capture(0, 500_000)
    stream.process_through(500_000, at_us=700_000)
    lag = stream.emit_partial(coverage_end_us=500_000, emitted_at_us=750_000)
    assert lag == 250_000
    stream.end_capture(at_us=500_000)
    assert stream.finalize(at_us=800_000) == 300_000
    assert stream.coverage_gaps == ()
    with pytest.raises(ASRError, match="already finalized"):
        stream.finalize(at_us=900_000)


def test_reconnect_replay_is_bounded_and_does_not_drop_accepted_audio():
    stream = ASRStream("dictation", replay_window_us=200_000, max_buffer_us=2_000_000)
    stream.capture(0, 1_000_000)
    assert stream.reconnect_from(850_000) == (800_000, 1_000_000)
    with pytest.raises(ASRError, match="outside bounded replay"):
        stream.reconnect_from(700_000)
    with pytest.raises(ASRError, match="contiguous"):
        stream.capture(1_100_000, 1_200_000)
