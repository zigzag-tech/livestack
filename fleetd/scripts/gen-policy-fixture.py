#!/usr/bin/env python3
"""Generate the policy-improver test fixture: a policy record stream written by
the broker's REAL write path (``PolicyRuntime`` + the native ``decide`` + the
native ``Recorder``), over a synthetic world in which one parameter change
truly improves the objective.

Why generated rather than hand-written: a record the improver may use must pass
the replay self-check (J§7.4) — rows, scores to 1e-12, and the blake3-seeded
exploration draw. Only the compiled family can produce that, so the fixture is
produced by it, once, and committed (gzipped).

The world: two RUNNING candidates. ``local`` is LOCAL and costs 1/h; ``spot`` is
SPOT and free. Under today's defaults they tie (w_budget*1 - w_resource*1 = 0
against 0) and the tie goes to ``local`` (earliest input position). ``spot`` is
really three times faster (job_wall_s ~20 s against ~60 s). Exploration
(epsilon 0.10, margin 0.25) sends ~5% of jobs to ``spot``, which is what lets
off-policy evaluation see that — and any candidate that breaks the tie toward
``spot`` (lower w_resource, higher w_budget, lower local_bonus) is a real
improvement. Both guardrails are flat (no lease expires, every caller reports
ok), so no candidate regresses them.

Every record also carries a SHADOW choice for one extra artifact (w_budget 1.5,
which picks ``spot``), so the improver's shadow-window evaluation has something
real to read.

Run (needs ``livestack_policy`` importable and the replay CLI for hashing):

    PYTHONPATH=node-py LIVESTACK_POLICY_BIN=.../livestack-policy \\
      python fleetd/scripts/gen-policy-fixture.py fleetd/src/policy/fixtures/stream
"""
import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

from livestack_node.policy_runtime import POLICY_ID, PolicyRuntime, defaults_artifact

T0 = 1_790_000_000.0          # 2026-09-21T11:33:20Z
N = 600                       # decisions, one every 10 minutes (~4.2 days)
SPACING_S = 600.0
SELF_PRINCIPAL = "livestack:fleetd"
SEED = 20260924


def seal(art: dict, workdir: str) -> dict:
    """The CLI computes the version (J§3.2: Python never hashes)."""
    path = os.path.join(workdir, "hash-me.json")
    with open(path, "w") as fh:
        json.dump(art, fh)
    out = subprocess.run([os.environ["LIVESTACK_POLICY_BIN"], "artifact", "hash", path],
                         check=True, capture_output=True, text=True).stdout
    return {**art, "version": json.loads(out)["version"]}


def candidate(cid: str, host: str, tier: str, cost_per_hour: float) -> dict:
    return {"id": cid, "features": {
        "host_id": host, "tier": tier, "running": True, "elastic": False,
        "selector_match": True, "fits_now": True, "headroom_ok": False,
        "fits_instance": False, "provision_latency_s": 0.0,
        "cost_per_hour": cost_per_hour, "cost_per_job": 0.0,
        "distance_ms": None, "utilization": None}}


def main(out_dir: str) -> None:
    rng = random.Random(SEED)
    work = tempfile.mkdtemp(prefix="policy-fixture-")
    policy_dir = os.path.join(work, "policy")
    rt = PolicyRuntime(policy_dir, mode="auto", self_principals=[SELF_PRINCIPAL])

    base = defaults_artifact()
    base["exploration"] = {"enabled": True, "epsilon": 0.10, "margin": 0.25}
    base["provenance"] = {"created_by": "human:fixture", "created_at": "2026-09-21T00:00:00Z",
                          "notes": "fleetd policy-improver test fixture"}
    active = seal(base, work)
    shadow = seal({**active, "params": {**active["params"], "w_budget": 1.5},
                   "parent_version": active["version"],
                   "provenance": {**active["provenance"], "notes": "fixture shadow"}}, work)
    rt.publish("active", active)
    rt.publish("shadow", [shadow])

    for i in range(N):
        ts = T0 + i * SPACING_S
        ctx = {"now": ts, "job": {"id": f"job-{i:04d}", "sla": "normal", "created_at": ts,
                                  "deadline": None, "est_duration_s": 60.0,
                                  "locality_host": None}}
        cands = [candidate("local", "zz-tower0", "LOCAL", 1.0),
                 candidate("spot", "heyuan-spot", "SPOT", 0.0)]
        decision_id = f"01FIXTURE{i:06d}"
        d = rt.decide(ctx, cands, decision_id)
        principal = SELF_PRINCIPAL if i % 50 == 7 else "acct_fixture"
        assert rt.record_decision(d, principal=principal, ts=ts), "recorder dropped a fixture record"
        wall = (20.0 if d["chosen"] == "spot" else 60.0) + rng.uniform(-5.0, 5.0)
        end = ts + wall
        for outcome, value in (("job_wall_s", wall), ("caller_ok", 1.0),
                               ("lease_held_s", wall + 1.0), ("lease_expired", 0.0)):
            assert rt.record_outcome(decision_id, outcome, value, source="fixture", ts=end)
    rt.close()

    os.makedirs(out_dir, exist_ok=True)
    stream = os.path.join(rt.records_dir, f"{POLICY_ID}.jsonl")
    # mtime=0: the same stream compresses to the same bytes, so a regeneration shows no diff.
    with open(stream, "rb") as src, gzip.GzipFile(
            os.path.join(out_dir, f"{POLICY_ID}.jsonl.gz"), "wb", compresslevel=9, mtime=0) as dst:
        shutil.copyfileobj(src, dst)
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump({"t0": T0, "decisions": N, "spacing_s": SPACING_S,
                   "self_principal": SELF_PRINCIPAL,
                   "active": active, "shadow": shadow}, fh, indent=2, sort_keys=True)
    shutil.rmtree(work)
    print(f"wrote {N} decisions to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1])
