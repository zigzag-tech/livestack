"""Golden corpus for the fleet scheduler's target choice (scheduler-policy-routine
task 1.1, design §3).

Recorded on the scheduler BEFORE `schedule()` was refactored to go through
`policy_runtime`, and committed with that recording. `test_policy_golden.py`
regenerates the same 5 000 states from the same seed and requires every output
line to be byte-identical, which is what proves the refactor preserved behaviour.

Run as a script to (re)write the corpus:

    python tests/policy_golden/generate.py          # from node-py/

Rewriting it after the refactor would defeat its purpose: do that only for a
deliberate, reviewed behaviour change, and say so in the commit.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import random
from typing import Iterator, List, Tuple

from livestack_node.fleet_scheduler import (
    Admit, CostModel, Deprovision, FleetPlan, FleetState, Job, Provision, Queue,
    Sla, Target, Tier, schedule,
)

SEED = 20260924
N_STATES = 5000
HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_PATH = os.path.join(HERE, "golden.jsonl.gz")

HOSTS = ("tower0", "xc-tower", "mac", "aliyun", "runpod")
LABELS = ({}, {"gpu": "a"}, {"gpu": "b"}, {"gpu": "a", "zone": "na"})
SELECTORS = ({}, {}, {}, {"gpu": "a"}, {"gpu": "b"}, {"zone": "na"})
# Few distinct values on purpose: ties in distance, utilization and cost are
# where a tie-break or a normalisation difference would show up.
DISTANCES = (None, None, 2.3, 16.2, 16.2, 120.0, 700.5)
UTILIZATIONS = (None, None, 0.0, 0.25, 0.5, 0.5, 1.0)
PER_HOUR = (0.0, 0.0, 0.5, 2.0, 20.0)
PER_JOB = (0.0, 0.0, 0.01, 0.3)
LATENCIES = (0.0, 10.0, 25.0, 90.0, 600.0, 4000.0)
OWNERS = ("acct_a", "acct_b", "corpus")
NOW = 1_788_600_000.0


def _target(rng: random.Random, i: int) -> Target:
    tier = rng.choice(list(Tier))
    shape = rng.random()
    running = shape < 0.55
    elastic = (not running and shape < 0.92) or (running and rng.random() < 0.2)
    if running:
        capacity = {"slot": rng.choice((0.0, 0.5, 1.0, 1.0, 2.0, 4.0))}
    else:
        capacity = {"slot": rng.choice((0.5, 1.0, 1.0, 2.0))}
    return Target(
        id=f"t{i}", host_id=rng.choice(HOSTS), tier=tier, capacity=capacity,
        cost=CostModel(per_hour=rng.choice(PER_HOUR), per_job=rng.choice(PER_JOB)),
        provision_latency_s=0.0 if running else rng.choice(LATENCIES),
        running=running, elastic=elastic,
        max_instances=rng.choice((0, 1, 1, 2, 3)),
        running_instances=rng.choice((0, 0, 1, 2)),
        up_since=NOW - rng.choice((0.0, 60.0, 300.0, 7200.0)),
        labels=dict(rng.choice(LABELS)),
        distance_ms=rng.choice(DISTANCES),
        utilization=rng.choice(UTILIZATIONS),
    )


def _job(rng: random.Random, i: int) -> Job:
    created = NOW - rng.choice((0.0, 5.0, 29.0, 31.0, 600.0, 1790.0, 1810.0, 40000.0))
    deadline = None
    if rng.random() < 0.3:
        deadline = NOW + rng.choice((-5.0, 0.0, 9.0, 60.0, 3600.0))
    return Job(
        id=f"j{i}", kind=rng.choice(("llm", "asr")),
        need={"slot": rng.choice((0.5, 1.0, 1.0, 2.0))},
        owner=rng.choice(OWNERS), created_at=created,
        sla=rng.choice(list(Sla)), deadline=deadline,
        est_duration_s=rng.choice((0.0, 5.0, 60.0, 3600.0)),
        selector=dict(rng.choice(SELECTORS)),
        locality_host=rng.choice((None, None) + HOSTS),
    )


def states(seed: int = SEED, n: int = N_STATES) -> Iterator[FleetState]:
    rng = random.Random(seed)
    for _ in range(n):
        targets = tuple(_target(rng, i) for i in range(rng.randint(0, 12)))
        jobs = tuple(_job(rng, i) for i in range(rng.randint(1, 6)))
        usage = {o: rng.randint(0, 3) for o in OWNERS if rng.random() < 0.4}
        yield FleetState(targets=targets, jobs=jobs, now=NOW, usage=usage)


def state_json(s: FleetState) -> dict:
    return {
        "now": s.now, "usage": dict(sorted(s.usage.items())),
        "targets": [{
            "id": t.id, "host_id": t.host_id, "tier": t.tier.name,
            "capacity": dict(t.capacity),
            "cost": {"per_hour": t.cost.per_hour, "per_job": t.cost.per_job},
            "provision_latency_s": t.provision_latency_s, "running": t.running,
            "elastic": t.elastic, "max_instances": t.max_instances,
            "running_instances": t.running_instances, "up_since": t.up_since,
            "labels": dict(t.labels), "distance_ms": t.distance_ms,
            "utilization": t.utilization} for t in s.targets],
        "jobs": [{
            "id": j.id, "kind": j.kind, "need": dict(j.need), "owner": j.owner,
            "created_at": j.created_at, "sla": j.sla.name, "deadline": j.deadline,
            "est_duration_s": j.est_duration_s, "selector": dict(j.selector),
            "locality_host": j.locality_host} for j in s.jobs],
    }


def action_tuple(a) -> list:
    """`[type, job_id, target_id|None, reason]` (Deprovision has no job)."""
    if isinstance(a, Admit):
        return ["Admit", a.job_id, a.target_id, a.reason]
    if isinstance(a, Provision):
        return ["Provision", a.job_id, a.target_id, a.reason]
    if isinstance(a, Queue):
        return ["Queue", a.job_id, None, a.reason]
    if isinstance(a, Deprovision):
        return ["Deprovision", None, a.target_id, a.reason]
    raise TypeError(a)


def record_line(s: FleetState, plan: FleetPlan) -> str:
    return json.dumps({"state": state_json(s), "summary": plan.summary(),
                       "actions": [action_tuple(a) for a in plan.actions]},
                      sort_keys=True, separators=(",", ":"))


def lines(seed: int = SEED, n: int = N_STATES) -> List[str]:
    return [record_line(s, schedule(s)) for s in states(seed, n)]


def read_golden(path: str = GOLDEN_PATH) -> List[str]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read().splitlines()


def write_golden(path: str = GOLDEN_PATH) -> Tuple[int, int]:
    body = ("\n".join(lines()) + "\n").encode("utf-8")
    buf = io.BytesIO()
    # mtime=0 so rewriting identical content produces an identical file.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(body)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return len(body), len(buf.getvalue())


if __name__ == "__main__":
    raw, packed = write_golden()
    print(f"wrote {GOLDEN_PATH}: {N_STATES} states, {raw} B raw, {packed} B gzipped")
