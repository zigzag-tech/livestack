"""Replay self-check over the broker's own policy stream (scheduler-policy-routine
task 4.3; Jingway design §7.4).

The real `PolicyRuntime`, with the real native module and its real `Recorder`,
decides every job of 1 000 seeded fleet states (the golden generator's) through
`schedule()` and records each committed decision exactly as hostd does, plus an
outcome per decision. The built `livestack-policy` CLI then replays the stream:
`selfcheck` must re-derive every logged decision (rows, greedy, chosen,
propensities) under the logging artifact, and report `passed`.

Three streams: the compiled defaults (no artifact file — which needs
`artifact_version` from the crate, J§3.2), an exploring artifact file in
`auto` mode (the native choice, explored draws included), and `compare` mode
(the reference acts; the record must still replay natively).

The CLI is `$LIVESTACK_POLICY_CLI`, else `native/policy/target/{release,debug}/
livestack-policy`. Skipped, with the reason named, when it or the module is absent.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from livestack_node.fleet_scheduler import schedule
from livestack_node.policy_runtime import (
    DEFAULT_PARAMS, POLICY_ID, PolicyRuntime, defaults_artifact, import_native,
)

from policy_golden.generate import states

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _cli():
    env = os.environ.get("LIVESTACK_POLICY_CLI")
    if env:
        return env
    for profile in ("release", "debug"):
        p = os.path.join(REPO, "native", "policy", "target", profile, "livestack-policy")
        if os.path.exists(p):
            return p
    return None


NATIVE = import_native()
CLI = _cli()
pytestmark = [
    pytest.mark.skipif(NATIVE is None, reason="livestack_policy not importable: "
                       "`maturin develop -m native/policy/py/Cargo.toml` (task 4.2)"),
    pytest.mark.skipif(CLI is None, reason="livestack-policy CLI not built: `cargo build "
                       "-p livestack-policy-cli` in native/policy, or set LIVESTACK_POLICY_CLI"),
]

N_STATES = 1000


def _versioned(art: dict) -> dict:
    art = dict(art, version="")
    art["version"] = NATIVE.m.artifact_version(art)
    return art


def _exploring_artifact() -> dict:
    params = dict(DEFAULT_PARAMS, w_distance=3.5, w_utilization=0.5)
    return _versioned(dict(defaults_artifact(), params=params,
                           exploration={"enabled": True, "epsilon": 0.1, "margin": 0.5},
                           provenance={"created_by": "human:test",
                                       "created_at": "2026-09-24T00:00:00Z"}))


def _record_stream(policy_dir: str, mode: str) -> PolicyRuntime:
    """Decide and record like hostd: committed choices only, one outcome each."""
    rt = PolicyRuntime(policy_dir, mode=mode, self_principals=("svc:improver",))
    assert type(rt.recorder).__name__ == "Recorder", rt.status()["records"]
    for n, s in enumerate(states(n=N_STATES)):
        ids = {j.id: f"s{n}-{j.id}" for j in s.jobs}
        plan = schedule(s, runtime=rt, decision_ids=ids)
        for d in plan.decisions.values():
            if rt.record_decision(d, principal="user:test", ts=1_788_600_000.0 + n):
                rt.record_outcome(d["decision_id"], "wall_s", 1.0 + n % 7,
                                  source="lease_release", ts=1_788_600_001.0 + n)
    return rt


def _selfcheck(policy_dir: str, store_dir: str) -> dict:
    p = subprocess.run(
        [CLI, "selfcheck", "--records",
         os.path.join(policy_dir, "records", f"{POLICY_ID}.jsonl*"),
         "--artifact-store", store_dir],
        capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\nstdout {p.stdout}\nstderr {p.stderr}"
    return json.loads(p.stdout)


def _store(tmp_path, *arts) -> str:
    d = tmp_path / "store"
    d.mkdir()
    for a in arts:
        (d / f"{a['version'][3:19]}.json").write_text(json.dumps(a))
    return str(d)


def _check(rt: PolicyRuntime, policy_dir: str, store: str, version: str) -> dict:
    rt.close()
    stats = rt.recorder.stats()
    assert stats["dropped"] == 0 and stats["last_error"] is None, stats
    lines = []
    for f in sorted(os.listdir(os.path.join(policy_dir, "records"))):
        with open(os.path.join(policy_dir, "records", f)) as fh:
            lines += [json.loads(x) for x in fh if x.strip()]
    decisions = [x for x in lines if x["record"] == "policy_decision"]
    assert {x["artifact_version"] for x in decisions} == {version}
    report = _selfcheck(policy_dir, store)
    print(f"selfcheck {version[:15]}: {report['self_check']} {report['checked']}/"
          f"{report['records']} checked, {len(lines) - len(decisions)} outcomes")
    assert report["self_check"] == "passed", report
    assert report["checked"] == report["records"] == len(decisions) > 1000, report
    assert report["failed"] == report["unreplayable"] == report["unparseable_lines"] == 0
    return report


def test_defaults_are_versioned_by_the_crate_and_decide_natively(tmp_path):
    rt = PolicyRuntime(str(tmp_path), mode="auto")
    st = rt.status()
    assert st["source"] == "defaults" and st["native"] is True
    assert st["active"]["version"] == _versioned(defaults_artifact())["version"]
    assert st["active"]["version"].startswith("b3:")
    assert "policy_native_unavailable" not in st["degraded"], st["degraded"]
    rt.close()


def test_selfcheck_passes_on_the_defaults_stream(tmp_path):
    d = str(tmp_path / "policy")
    rt = _record_stream(d, "auto")
    defaults = _versioned(defaults_artifact())
    _check(rt, d, _store(tmp_path, defaults), defaults["version"])


def test_selfcheck_passes_on_an_exploring_artifact_stream(tmp_path):
    d = tmp_path / "policy"
    d.mkdir()
    art = _exploring_artifact()
    (d / f"{POLICY_ID}.active.json").write_text(json.dumps(art))
    rt = _record_stream(str(d), "auto")
    assert rt.status()["exploration"] == "enabled"
    _check(rt, str(d), _store(tmp_path, art), art["version"])
    explored = sum(json.loads(x)["explored"] for f in os.listdir(d / "records")
                   for x in open(d / "records" / f) if '"policy_decision"' in x)
    assert explored > 0, "no explored decision: the stream did not exercise exploration"


def test_selfcheck_passes_on_a_compare_mode_stream(tmp_path):
    # The reference acts and its rows are recorded; the native replay must
    # reproduce them exactly (reason strings included) or rollout step 6.3
    # would start from records that cannot be self-checked.
    d = str(tmp_path / "policy")
    rt = _record_stream(d, "compare")
    assert rt.mismatches == 0
    defaults = _versioned(defaults_artifact())
    _check(rt, d, _store(tmp_path, defaults), defaults["version"])


def test_selfcheck_fails_on_a_tampered_record(tmp_path):
    # Positive control: an instrument that cannot fail proves nothing.
    d = str(tmp_path / "policy")
    rt = _record_stream(d, "auto")
    rt.close()
    rec_dir = os.path.join(d, "records")
    path = os.path.join(rec_dir, sorted(os.listdir(rec_dir))[0])
    with open(path) as fh:
        lines = fh.read().splitlines()
    for i, x in enumerate(lines):
        r = json.loads(x)
        if r["record"] == "policy_decision" and len(r["explore_set"]) == 1 \
                and sum(row["eligible"] for row in r["rows"]) > 1:
            other = next(row["id"] for row in r["rows"]
                         if row["eligible"] and row["id"] != r["chosen"])
            r["chosen"] = r["greedy"] = other
            lines[i] = json.dumps(r)
            break
    else:
        pytest.fail("no record with two eligible candidates to tamper with")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    defaults = _versioned(defaults_artifact())
    p = subprocess.run([CLI, "selfcheck", "--records", os.path.join(rec_dir, f"{POLICY_ID}.jsonl*"),
                        "--artifact-store", _store(tmp_path, defaults)],
                       capture_output=True, text=True)
    assert p.returncode == 3, (p.returncode, p.stdout, p.stderr)
    report = json.loads(p.stdout)
    assert report["self_check"] == "failed" and report["failed"] == 1
