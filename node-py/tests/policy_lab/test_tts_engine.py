import pytest

from livestack_node.policy_lab.tts_engine import TTSChunk, TTSError, TTSStream


def _chunk(chunk_id, sentence, start, duration, received, voice="voice", model="model"):
    return TTSChunk(
        chunk_id=chunk_id,
        sentence_id=sentence,
        audio_start_us=start,
        audio_duration_us=duration,
        synthesized_at_us=max(0, received - 10_000),
        received_at_us=received,
        voice_compatibility_id=voice,
        model_revision=model,
    )


def test_s10_next_sentence_overlap_avoids_gap_when_received_during_playback():
    stream = TTSStream("utterance", initial_buffer_us=100_000, voice_compatibility_id="voice", model_revision="model")
    stream.add_chunk(_chunk("one", "sentence-1", 0, 1_000_000, 200_000))
    stream.add_chunk(_chunk("two", "sentence-2", 1_000_000, 1_000_000, 800_000))
    timeline = stream.timeline()
    assert timeline.first_audible_us == 200_000
    assert timeline.completed_at_us == 2_200_000
    assert timeline.total_underrun_us == 0
    assert timeline.sentence_gaps_us == {"sentence-2": 0}


def test_s10_late_second_sentence_has_exact_underrun_and_gap():
    stream = TTSStream("utterance", initial_buffer_us=100_000, voice_compatibility_id="voice", model_revision="model")
    stream.add_chunk(_chunk("one", "sentence-1", 0, 1_000_000, 200_000))
    stream.add_chunk(_chunk("two", "sentence-2", 1_000_000, 1_000_000, 1_500_000))
    timeline = stream.timeline()
    assert timeline.underruns == ((1_200_000, 1_500_000),)
    assert timeline.total_underrun_us == 300_000
    assert timeline.sentence_gaps_us == {"sentence-2": 300_000}
    assert timeline.completed_at_us == 2_500_000


def test_s11_cancel_stops_playback_without_replaying_committed_audio():
    stream = TTSStream("utterance", initial_buffer_us=100_000, voice_compatibility_id="voice", model_revision="model")
    stream.add_chunk(_chunk("one", "sentence-1", 0, 1_000_000, 200_000))
    stream.add_chunk(_chunk("two", "sentence-2", 1_000_000, 1_000_000, 800_000))
    stream.cancel(at_us=600_000)
    timeline = stream.timeline()
    assert timeline.completed_at_us == 600_000
    assert timeline.audible_audio_us == 400_000
    assert timeline.canceled_audio_leak_us == 0
    with pytest.raises(TTSError, match="replay committed"):
        stream.add_failover_chunk(_chunk("retry", "sentence-1", 0, 100_000, 610_000))


def test_voice_and_model_affinity_hold_for_whole_utterance():
    stream = TTSStream("utterance", initial_buffer_us=0, voice_compatibility_id="voice", model_revision="model")
    with pytest.raises(TTSError, match="affinity"):
        stream.add_chunk(_chunk("bad", "sentence", 0, 100_000, 0, voice="other"))
