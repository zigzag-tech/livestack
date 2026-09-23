"""Adapt durable worker claims to Harmony's existing pure fleet scheduler.

Called only inside the authority's immediate transaction. Assign one attempt per
worker initially; all environments on the same physical host share claims.
"""
import json
import uuid

from ..fleet_scheduler import Admit, FleetState, Job, Sla, Target, Tier, schedule
from .model import encode


def place(db, now, limits, principals=None):
    workers = db.execute("SELECT * FROM workers WHERE ready=1 AND seen>? ORDER BY id",
                         (now-limits.fresh_seconds,)).fetchall()
    reports = {w["id"]: json.loads(w["report"]) for w in workers}
    # A cleanup hold keeps charging its host until the worker acknowledges it,
    # deliberately: the attempt's containers may still be consuming that host
    # (see the store module docstring). But a worker that is GONE never
    # acknowledges, and nothing else releases the hold, so the charge became
    # permanent.
    #
    # Measured 2026-09-22: `xc-mac-studio-harmony` had held one for 26 hours
    # with its lease 26 hours expired. It stayed marked busy and its host
    # stayed short that attempt's vector for a day.
    #
    # Holding capacity while a worker might still be running the attempt is
    # caution; holding it forever is a leak that denies a shared host to
    # everyone. Worker liveness is the evidence, and `cleanup_seconds` is far
    # longer than any plausible cleanup, so this drops only holds that nobody
    # can still be honouring.
    #
    # The attempt STAYS in `cleanup`. The obligation and the reservation are
    # different things wearing one state: a worker that returns is still told
    # to clean up (`register` reports it), which is what actually stops its
    # containers.
    active = db.execute(
        "SELECT a.* FROM attempts a JOIN workers w ON w.id=a.worker "
        "WHERE a.state IN ('running','cleanup') "
        "AND NOT (a.state='cleanup' AND w.seen<?)",
        (now-limits.cleanup_seconds,)).fetchall()
    used, busy = {}, set()
    # Per-principal concurrency cap: running attempts per job owner, counted
    # before the queued loop. A capped owner is skipped, never a blocker —
    # later owners' jobs still place.
    running = {}
    for a in active:
        if a["state"] != "running":
            continue
        job_row = db.execute("SELECT owner FROM jobs WHERE id=?", (a["job"],)).fetchone()
        if job_row:
            running[job_row["owner"]] = running.get(job_row["owner"], 0) + 1
    caps = {pid: p.max_running for pid, p in (principals or {}).items()
            if getattr(p, "max_running", None) is not None}
    for a in active:
        # attempts.need stores what the attempt reserved, which is its admission
        # vector; a burstable attempt is not charged for capacity it may not use.
        busy.add(a["worker"])
        for key, value in json.loads(a["need"]).items():
            used.setdefault(a["host"], {}).setdefault(key, 0)
            used[a["host"]][key] += value
    # Two different quantities, previously collapsed into one minimum.
    #
    # `worker_free` is what THIS worker may take: its configured capacity,
    # clamped by what it observes free. `host_free` is what the HOST has: every
    # worker on a host measures the same physical machine, so the LEAST-clamped
    # observation is the truest one, not the most-clamped.
    #
    # Taking the elementwise minimum conflated "this worker is small" with
    # "this host is full", because a worker's reported availability is already
    # clamped to its own capacity by `WorkloadWorker.report`. A deliberately
    # small worker therefore dragged the whole host down to its size.
    #
    # Measured on xc-tower-ubuntu 2026-09-22: a 1 GiB policy-lab worker pinned
    # host_free.disk to 1 GiB on a machine with 560 GiB free, while three other
    # workers on that same host each reported 142-192 GiB. Every 16 GiB E2E
    # admission was refused as "insufficient shared host resources", so the
    # fleet's E2E gate was unreachable for a day by a worker serving an
    # unrelated handler.
    #
    # Double counting is still prevented, by the mechanism that actually
    # prevents it: `used` sums every active attempt's reservation PER HOST and
    # is subtracted from both quantities, so a second admission on a host sees
    # the first one's claim. The minimum was belt-and-braces on top of that,
    # and it is what broke.
    #
    # Both bounds are now tested at placement: a job must fit the host AND the
    # worker it would run on. The old code checked only the (collapsed) host
    # figure, which was safe only because the minimum happened to include the
    # smallest worker.
    worker_free, host_observed = {}, {}
    for w in workers:
        report = reports[w["id"]]
        observed = {k: min(v, report["available"].get(k, 0)) for k, v in report["capacity"].items()}
        reserved = used.get(w["host"], {})
        worker_free[w["id"]] = {k: max(0, v - reserved.get(k, 0)) for k, v in observed.items()}
        previous = host_observed.get(w["host"])
        host_observed[w["host"]] = observed if previous is None else {
            k: max(previous.get(k, 0), observed.get(k, 0)) for k in previous.keys() | observed.keys()}
    host_free = {h: {k: max(0, v - used.get(h, {}).get(k, 0)) for k, v in obs.items()}
                 for h, obs in host_observed.items()}
    # Priority is caller intent, while Harmony still owns capability/resource
    # admission and the final worker choice. Legacy persisted specs omit the
    # field and retain their original priority-zero FIFO behavior.
    for row in db.execute("SELECT * FROM jobs WHERE state='queued' "
                          "ORDER BY COALESCE(json_extract(spec,'$.priority'),0) DESC, created, id").fetchall():
        spec = json.loads(row["spec"])
        cap = caps.get(row["owner"])
        if cap is not None and running.get(row["owner"], 0) >= cap:
            db.execute("UPDATE jobs SET reason=? WHERE id=?",
                       (f"principal at max_running ({cap})", row["id"]))
            continue
        # Admission and execution are separate quantities. `admit` is both the
        # fit test and the reservation, so admitted vectors on a host always sum
        # within its capacity; `need` never enters placement and only caps the
        # attempt's cgroup. Omitting `admit` submits need as both, which is
        # exactly the behavior every existing caller already has.
        admit = spec.get("admit") or spec["need"]
        targets = []
        rejected = []
        compatible = [w for w in workers if spec["handler"] in reports[w["id"]]["handlers"]]
        for w in compatible:
            report = reports[w["id"]]
            reason = None
            if w["id"] in busy:
                reason = "worker holds an active attempt or cleanup"
            elif any(report["labels"].get(k) != v for k, v in spec["selector"].items()):
                reason = "required capability absent"
            elif any(min(host_free[w["host"]].get(k, 0), worker_free[w["id"]].get(k, 0)) < n
                     for k, n in admit.items()):
                # One reason for both bounds: a caller can act on neither
                # differently, and the distinct figures are already in the
                # worker report the refusal is recorded against.
                reason = "insufficient shared host resources"
            if reason:
                rejected.append({"worker": w["id"], "reason": reason})
                continue
            targets.append(Target(id=w["id"], host_id=w["host"], tier=Tier.LOCAL,
                                  capacity=host_free[w["host"]], labels=report["labels"]))
        job = Job(id=row["id"], kind=spec["handler"], owner=row["owner"], need=admit,
                  created_at=row["created"], sla=Sla.BATCH, deadline=spec["deadline"],
                  est_duration_s=spec["estimate_seconds"], selector=spec["selector"],
                  locality_host=spec["locality_host"])
        grants = schedule(FleetState(targets=tuple(targets), jobs=(job,), now=now)).of(Admit)
        if not grants:
            if not workers:
                reason = "no fresh, reconciled worker"
            elif not compatible:
                reason = f"no fresh worker advertises handler {spec['handler']}"
            else:
                reason = encode(rejected or {"reason": "no target can meet deadline"})
            db.execute("UPDATE jobs SET reason=? WHERE id=?", (reason[:8192], row["id"]))
            continue
        chosen = next(w for w in workers if w["id"] == grants[0].target_id)
        fence = row["fence"] + 1
        aid = uuid.uuid4().hex
        db.execute("INSERT INTO attempts(id,job,worker,boot,host,fence,state,need,expires,created) "
                   "VALUES(?,?,?,?,?,?,'running',?,?,?)",
                   (aid, row["id"], chosen["id"], chosen["boot"], chosen["host"], fence,
                    encode(admit), now+limits.lease_seconds, now))
        db.execute("UPDATE jobs SET state='running',fence=?,updated=?,reason=? WHERE id=?",
                   (fence, now, grants[0].reason, row["id"]))
        busy.add(chosen["id"])
        running[row["owner"]] = running.get(row["owner"], 0) + 1
        for k, n in admit.items():
            host_free[chosen["host"]][k] -= n
