import json

from livestack_node.policy_lab.emitter import BoundedEmitter, SegmentedSpool


def test_sensitive_or_unknown_metadata_never_reaches_output():
    emitter = BoundedEmitter(max_queue_bytes=4096, max_event_bytes=2048)
    assert not emitter.offer(
        "decision",
        {"request_id": "r1", "prompt": "TOP SECRET prompt contents"},
        now_us=1,
    )
    assert not emitter.offer(
        "decision",
        {"request_id": "r1", "worker_id": "https://private.example/token"},
        now_us=2,
    )
    output = b"".join(emitter.drain())
    assert b"TOP SECRET" not in output
    assert b"private.example" not in output
    assert emitter.rejected_events == 2


def test_s22_saturation_is_nonblocking_and_emits_gap_after_space_returns():
    emitter = BoundedEmitter(max_queue_bytes=300, max_event_bytes=256)
    accepted = 0
    for index in range(1_000):
        accepted += emitter.offer(
            "stream_progress",
            {"request_id": "r1", "produced_units": index, "coverage_us": index},
            now_us=index,
        )
    assert accepted < 1_000
    assert emitter.dropped_events > 0
    assert emitter.producer_io_calls == 0

    emitter.drain()
    assert emitter.offer(
        "request_terminal",
        {"request_id": "r1", "outcome": "completed"},
        now_us=2_000,
    )
    rows = [json.loads(item) for item in emitter.drain()]
    assert rows[0]["kind"] == "evidence_gap"
    assert rows[0]["dropped_count"] > 0
    assert rows[-1]["event_type"] == "request_terminal"


def test_segmented_spool_is_bounded_and_never_deletes_to_make_room(tmp_path):
    spool = SegmentedSpool(tmp_path / "spool", max_bytes=25, segment_bytes=10)
    assert spool.append(b"123456789\n")
    assert spool.append(b"abcdefghi\n")
    before = sorted(path.read_bytes() for path in (tmp_path / "spool").iterdir())
    assert not spool.append(b"overflow!!\n")
    after = sorted(path.read_bytes() for path in (tmp_path / "spool").iterdir())
    assert before == after
    assert spool.refused_records == 1
