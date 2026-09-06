"""The decision ledger: schema, the bounded writer, and what "sufficient" means.

The requirement these pin down (`_plans/decision-ledger.md` §1): a reader who was
not present must be able to answer, from the record alone and with no live
system, what was asked / what was known / what was chosen and why / **why each
loser lost** / what happened. The fourth is the one that is normally missing, and
it is the one that lets a future agent say "it should have gone to X".
"""
import json
import os
import time

from livestack_node.ledger import (
    Candidate, Decision, JsonlLedger, MAX_RECORD_BYTES, distance_band,
    ledger_from_env, new_decision_id, validate,
)


def _decision(**over) -> Decision:
    base = dict(
        emitter="fleet-broker", emitter_id="xc-tower-ubuntu:8801",
        kind="asr", decision="rank",
        request={"owner": "media-corpus", "sla": "batch", "vantage": "host:zz-tower0",
                 "region": "asia-cn", "policy_regions": ["asia-cn", "na"]},
        candidates=[
            Candidate(id="xc-tower-ubuntu-asr", host_id="xc-tower-ubuntu",
                      device_id="xc-tower-ubuntu/3f9a1c22", state="fresh", ready=True,
                      distance_ms=640.2, resident=True, region="na", inputs_at=1788599998.9,
                      load={"in_flight": 0, "pressure": 0.21, "source": "server"},
                      outcome="chosen", rank=1, reason="band>=600; in_flight=0"),
            Candidate(id="zz-tower0-asr", host_id="zz-tower0", state="fresh", ready=True,
                      distance_ms=12.0, region="asia-cn", inputs_at=1788599998.1,
                      load={"in_flight": 3, "pressure": 0.81, "source": "server"},
                      outcome="ranked", rank=2,
                      reason="band<50 but in_flight=3 pressure=0.81"),
            Candidate(id="xc-mac-studio-asr", state="suspect", ready=False,
                      outcome="filtered", reason="filtered: state=suspect"),
        ],
        chosen="xc-tower-ubuntu-asr",
        reason="nearest fresh candidate with lowest load; zz-tower0 saturated",
        ttl_s=60,
    )
    base.update(over)
    return Decision(**base)


# -- identity -----------------------------------------------------------------

def test_a_decision_id_is_sortable_and_unique():
    """It is carried with the request across processes and the outcome is joined
    back to it later, so it must be comparable without a lookup."""
    ids = [new_decision_id(now=1788600000 + i) for i in range(5)]
    assert ids == sorted(ids), "ULIDs must sort by time"
    assert len({new_decision_id() for _ in range(1000)}) == 1000
    assert all(len(i) == 26 for i in ids)


def test_ids_stay_ordered_inside_one_millisecond():
    """A PLAIN ULID is not ordered here — inside a millisecond the random
    component decides, so two records written in the same millisecond sort
    arbitrarily. That shows up the moment anything replays a ledger and expects
    the order it was written in, which the retrospective does."""
    burst = [new_decision_id(now=1788600000.0) for _ in range(200)]
    assert burst == sorted(burst)
    assert len(set(burst)) == 200


def test_a_clock_that_goes_backwards_does_not_reorder_history():
    a = new_decision_id(now=1788600000.0)
    b = new_decision_id(now=1788599000.0)     # NTP step backwards
    assert b > a, "ordering is the promise; the timestamp is for reading"


def test_distance_is_recorded_in_bands():
    # A client picker re-measures the fine detail itself; the fleet's job is to
    # keep a request from STARTING on the wrong continent. Bands also keep
    # ranking stable under jitter, which raw millis are not.
    assert distance_band(2) == "<50"
    assert distance_band(49.9) == "<50"
    assert distance_band(180) == "<200"
    assert distance_band(599) == "<600"
    assert distance_band(1089) == ">=600"
    assert distance_band(None) == "unknown"


# -- the schema ---------------------------------------------------------------

def test_a_full_record_validates():
    assert validate(_decision().to_dict()) == []


def test_a_candidate_without_a_reason_is_not_a_decision_record():
    """A record that cannot say why a loser lost is a log line. The whole point
    is the field that lets a reader check the verdict."""
    d = _decision(candidates=[Candidate(id="a", outcome="ranked", reason="")])
    problems = validate(d.to_dict())
    assert any("reason" in p for p in problems), problems


def test_a_filtered_candidate_is_a_row_not_an_omission():
    """The mac's 7-hour outage would have appeared as
    `filtered: state=fresh, ready=false` — or, before the membership fix, as a
    CHOSEN candidate whose outcome was `failed`, which is exactly the record
    that would have caught the roster lie."""
    rows = _decision().to_dict()["candidates"]
    assert len(rows) == 3
    assert rows[0]["outcome"] == "filtered", "eliminations sort first"
    assert rows[0]["reason"]


def test_candidates_carry_values_not_references():
    """A reader must never need the live fleet to interpret a record, and
    `inputs_at` makes staleness a first-class fact — a rank built on a 40 s old
    load reading is a different decision from one built on a 2 s old one."""
    row = next(c for c in _decision().to_dict()["candidates"] if c["id"] == "zz-tower0-asr")
    assert row["load"] == {"in_flight": 3, "pressure": 0.81, "source": "server"}
    assert row["distance_ms"] == 12.0
    assert row["distance_band"] == "<50"
    assert row["inputs_at"] == 1788599998.1


def test_completeness_every_candidate_has_an_outcome_and_a_reason():
    rows = _decision().to_dict()["candidates"]
    assert len(rows) == 3
    assert all(r["outcome"] in ("chosen", "ranked", "filtered") for r in rows)
    assert all(r["reason"] for r in rows)


# -- bounds (rule 10: name the bound and the enforcer) ------------------------

def test_the_writer_rotates_at_the_configured_size(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p, max_bytes=2000, max_files=3)
    for _ in range(40):
        led.append(_decision())
    assert os.path.getsize(p) <= 2000 + MAX_RECORD_BYTES
    assert os.path.exists(p + ".1")
    # max_files=3 means the live file plus .1 and .2, and nothing beyond.
    assert not os.path.exists(p + ".3")


def test_an_unset_age_window_deletes_nothing(tmp_path):
    """A delete-shaped bound must never default to deleting — the same rule the
    peer roster's prune window follows, for the same reason."""
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p, max_bytes=500, max_files=3, max_age_s=None)
    for _ in range(30):
        led.append(_decision())
    os.utime(p + ".1", (0, 0))       # ancient
    assert led._prune_old() == []
    assert os.path.exists(p + ".1")


def test_the_age_window_prunes_rotated_files_when_it_is_set(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p, max_bytes=500, max_files=3, max_age_s=86400)
    for _ in range(30):
        led.append(_decision())
    assert os.path.exists(p + ".1")
    os.utime(p + ".1", (0, 0))
    assert led._prune_old() == [p + ".1"]
    assert not os.path.exists(p + ".1")


def test_a_huge_candidate_list_is_truncated_rather_than_written(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p)
    big = _decision(candidates=[
        Candidate(id=f"n{i}", outcome="ranked", rank=i, reason="x" * 400)
        for i in range(500)
    ])
    written = led.append(big)
    assert written["truncated"] > 0
    assert len(json.dumps(written, separators=(",", ":"))) <= MAX_RECORD_BYTES
    # The winner and the near-misses survive; the far tail is what is shed.
    assert written["candidates"][0]["id"] == "n0"


def test_a_ledger_that_cannot_write_does_not_take_the_decision_with_it(tmp_path):
    """Emitting is observability. A full disk degrades to silence, once — it is
    never worth failing a placement for."""
    lines = []
    led = JsonlLedger(str(tmp_path / "d.jsonl"), log=lines.append)
    led.path = str(tmp_path / "no-such-dir" / "d.jsonl")
    assert led.append(_decision()) is None
    assert len(lines) == 1 and "write failed" in lines[0]


def test_the_env_can_disable_emission_entirely(monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER", "0")
    assert ledger_from_env("fleet-decisions", 64) is None


def test_the_env_age_window_is_unset_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVESTACK_LEDGER", raising=False)
    monkeypatch.delenv("LIVESTACK_LEDGER_AGE_DAYS", raising=False)
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    led = ledger_from_env("fleet-decisions", 64)
    assert led is not None and led.max_age_s is None
    assert led.max_bytes == 64 * 1024 * 1024


# -- reading ------------------------------------------------------------------

def test_records_read_back_oldest_first_across_rotations(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p, max_bytes=1500, max_files=4)
    written = [led.append(_decision())["decision_id"] for _ in range(30)]
    got = [r["decision_id"] for r in led.read()]
    # Some of the oldest may have rotated off the end; what survives is in order
    # and is a suffix of what was written.
    assert got == [d for d in written if d in set(got)]
    assert got == sorted(got)


def test_a_truncated_line_is_skipped_rather_than_raising(tmp_path):
    """A ledger truncated by a crash mid-write must still be readable, or the
    one record that explains the crash is the one nobody can get to."""
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p)
    led.append(_decision())
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"decision_id": "half-writ')
    led.append(_decision())
    assert len(led.read()) == 2


def test_read_filters_by_time_and_kind(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = JsonlLedger(p)
    old = _decision(kind="asr")
    old.ts = time.time() - 3600
    led.append(old)
    led.append(_decision(kind="tts"))
    assert len(led.read(since=time.time() - 60)) == 1
    assert len(led.read(kind="asr")) == 1
