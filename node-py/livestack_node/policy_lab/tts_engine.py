"""Streaming TTS synthesis/receipt/buffering/playback timeline model."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError


class TTSError(ContractError):
    pass


@dataclass(frozen=True)
class TTSChunk:
    chunk_id: str
    sentence_id: str
    audio_start_us: int
    audio_duration_us: int
    synthesized_at_us: int
    received_at_us: int
    voice_compatibility_id: str
    model_revision: str


@dataclass(frozen=True)
class TTSTimeline:
    first_audible_us: int | None
    completed_at_us: int | None
    audible_audio_us: int
    underruns: tuple[tuple[int, int], ...]
    total_underrun_us: int
    sentence_gaps_us: dict[str, int]
    canceled_audio_leak_us: int


class TTSStream:
    def __init__(
        self,
        utterance_id: str,
        *,
        initial_buffer_us: int,
        voice_compatibility_id: str,
        model_revision: str,
    ) -> None:
        if not utterance_id or initial_buffer_us < 0 or not voice_compatibility_id or not model_revision:
            raise TTSError("invalid TTS stream parameters")
        self.utterance_id = utterance_id
        self.initial_buffer_us = initial_buffer_us
        self.voice_compatibility_id = voice_compatibility_id
        self.model_revision = model_revision
        self._chunks: list[TTSChunk] = []
        self.canceled_at_us: int | None = None
        self.committed_audio_us = 0

    def add_chunk(self, chunk: TTSChunk) -> None:
        if (
            chunk.voice_compatibility_id != self.voice_compatibility_id
            or chunk.model_revision != self.model_revision
        ):
            raise TTSError("TTS voice/model affinity mismatch")
        expected_start = sum(item.audio_duration_us for item in self._chunks)
        if chunk.audio_start_us != expected_start or chunk.audio_duration_us <= 0:
            raise TTSError("TTS chunks must provide contiguous positive audio")
        if chunk.synthesized_at_us < 0 or chunk.received_at_us < chunk.synthesized_at_us:
            raise TTSError("invalid synthesis/receipt timing")
        if any(item.chunk_id == chunk.chunk_id for item in self._chunks):
            raise TTSError("duplicate TTS chunk")
        self._chunks.append(chunk)

    def add_failover_chunk(self, chunk: TTSChunk) -> None:
        if chunk.audio_start_us < self.committed_audio_us:
            raise TTSError("failover would replay committed audible audio")
        self.add_chunk(chunk)

    def cancel(self, *, at_us: int) -> None:
        if self.canceled_at_us is not None or at_us < 0:
            raise TTSError("invalid or duplicate TTS cancellation")
        self.canceled_at_us = at_us

    def _first_audible(self) -> int | None:
        if not self._chunks:
            return None
        required = self.initial_buffer_us
        accumulated = 0
        required_chunks: list[TTSChunk] = []
        for chunk in self._chunks:
            required_chunks.append(chunk)
            accumulated += chunk.audio_duration_us
            if accumulated >= required:
                return max(item.received_at_us for item in required_chunks)
        return None

    def timeline(self) -> TTSTimeline:
        first = self._first_audible()
        if first is None or (self.canceled_at_us is not None and self.canceled_at_us <= first):
            completed = self.canceled_at_us if self.canceled_at_us is not None else None
            return TTSTimeline(first, completed, 0, (), 0, {}, 0)
        wall_cursor = first
        audible = 0
        underruns: list[tuple[int, int]] = []
        sentence_gaps: dict[str, int] = {}
        previous_sentence = self._chunks[0].sentence_id
        for chunk in self._chunks:
            if self.canceled_at_us is not None and wall_cursor >= self.canceled_at_us:
                break
            stall = 0
            if chunk.received_at_us > wall_cursor:
                stall_end = chunk.received_at_us
                if self.canceled_at_us is not None:
                    stall_end = min(stall_end, self.canceled_at_us)
                if stall_end > wall_cursor:
                    underruns.append((wall_cursor, stall_end))
                    stall = stall_end - wall_cursor
                    wall_cursor = stall_end
            if chunk.sentence_id != previous_sentence:
                sentence_gaps[chunk.sentence_id] = stall
                previous_sentence = chunk.sentence_id
            if self.canceled_at_us is not None and wall_cursor >= self.canceled_at_us:
                break
            playable = chunk.audio_duration_us
            if self.canceled_at_us is not None:
                playable = min(playable, self.canceled_at_us - wall_cursor)
            audible += playable
            wall_cursor += playable
            self.committed_audio_us = audible
            if playable < chunk.audio_duration_us:
                break
        completed = self.canceled_at_us if self.canceled_at_us is not None else wall_cursor
        return TTSTimeline(
            first_audible_us=first,
            completed_at_us=completed,
            audible_audio_us=audible,
            underruns=tuple(underruns),
            total_underrun_us=sum(end - start for start, end in underruns),
            sentence_gaps_us=sentence_gaps,
            canceled_audio_leak_us=0,
        )
