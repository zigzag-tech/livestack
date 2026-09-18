"""Coverage-conserving incremental ASR stream model."""

from __future__ import annotations

from .contracts import ContractError


class ASRError(ContractError):
    pass


class ASRStream:
    def __init__(self, stream_id: str, *, replay_window_us: int, max_buffer_us: int) -> None:
        if not stream_id or replay_window_us < 0 or max_buffer_us <= 0:
            raise ASRError("invalid ASR stream parameters")
        self.stream_id = stream_id
        self.replay_window_us = replay_window_us
        self.max_buffer_us = max_buffer_us
        self.captured_until_us = 0
        self.processed_until_us = 0
        self.capture_ended_at_us: int | None = None
        self.finalized_at_us: int | None = None
        self.partial_lags_us: list[int] = []

    @property
    def accepted_coverage_us(self) -> int:
        return self.captured_until_us

    @property
    def processed_coverage_us(self) -> int:
        return self.processed_until_us

    @property
    def backlog_us(self) -> int:
        return self.captured_until_us - self.processed_until_us

    @property
    def coverage_gaps(self) -> tuple[tuple[int, int], ...]:
        return ()

    def capture(self, start_us: int, end_us: int) -> None:
        if self.capture_ended_at_us is not None:
            raise ASRError("capture already ended")
        if start_us != self.captured_until_us or end_us <= start_us:
            raise ASRError("accepted audio must be contiguous and positive")
        if end_us - self.processed_until_us > self.max_buffer_us:
            raise ASRError("accepted audio exceeds bounded buffer; admission must stop first")
        self.captured_until_us = end_us

    def process_through(self, coverage_end_us: int, *, at_us: int) -> None:
        if coverage_end_us > self.captured_until_us:
            raise ASRError("cannot process uncaptured future audio")
        if coverage_end_us < self.processed_until_us:
            raise ASRError("processing coverage cannot move backwards")
        if at_us < coverage_end_us:
            raise ASRError("processing cannot complete before capture")
        self.processed_until_us = coverage_end_us

    def emit_partial(self, *, coverage_end_us: int, emitted_at_us: int) -> int:
        if coverage_end_us > self.processed_until_us or emitted_at_us < coverage_end_us:
            raise ASRError("partial exceeds processed coverage or precedes capture")
        lag = emitted_at_us - coverage_end_us
        self.partial_lags_us.append(lag)
        return lag

    def end_capture(self, *, at_us: int) -> None:
        if at_us != self.captured_until_us:
            raise ASRError("capture end must match accepted coverage")
        self.capture_ended_at_us = at_us

    def finalize(self, *, at_us: int) -> int:
        if self.finalized_at_us is not None:
            raise ASRError("stream already finalized")
        if self.capture_ended_at_us is None or self.processed_until_us != self.captured_until_us:
            raise ASRError("cannot finalize before all accepted audio is processed")
        if at_us < self.capture_ended_at_us:
            raise ASRError("finalization precedes last capture")
        self.finalized_at_us = at_us
        return at_us - self.capture_ended_at_us

    def reconnect_from(self, requested_coverage_us: int) -> tuple[int, int]:
        earliest = max(0, self.captured_until_us - self.replay_window_us)
        if requested_coverage_us < earliest or requested_coverage_us > self.captured_until_us:
            raise ASRError("reconnect request is outside bounded replay window")
        return earliest, self.captured_until_us
