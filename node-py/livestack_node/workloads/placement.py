"""Adapt durable worker claims to Harmony's existing pure fleet scheduler.

Called only inside the authority's immediate transaction. Assign one attempt per
worker initially; all environments on the same physical host share claims.
"""
import json
import uuid

from ..fleet_scheduler import Admit, FleetState, Job, Sla, Target, Tier, schedule
from .model import encode


def place(db, now, limits):
    workers = db.execute("SELECT * FROM workers WHERE ready=1 AND seen>? ORDER BY id",
                         (now-limits.fresh_seconds,)).fetchall()
    reports = {w["id"]: json.loads(w["report"]) for w in workers}
    active = db.execute("SELECT * FROM attempts WHERE state IN ('running','cleanup')").fetchall()
    used, busy = {}, set()
    for a in active:
        busy.add(a["worker"])
        for key, value in json.loads(a["need"]).items():
            used.setdefault(a["host"], {}).setdefault(key, 0)
            used[a["host"]][key] += value
    # Conservative intersection across execution environments on one physical
    # host: WSL and Windows cannot advertise two independent copies of its RAM.
    host_free = {}
    for w in workers:
        report = reports[w["id"]]
        free = {k: max(0, min(v, report["available"].get(k, 0)) - used.get(w["host"], {}).get(k, 0))
                for k, v in report["capacity"].items()}
        previous = host_free.get(w["host"])
        host_free[w["host"]] = free if previous is None else {
            k: min(previous.get(k, 0), free.get(k, 0)) for k in previous.keys() | free.keys()}
    # Priority is caller intent, while Harmony still owns capability/resource
    # admission and the final worker choice. Legacy persisted specs omit the
    # field and retain their original priority-zero FIFO behavior.
    for row in db.execute("SELECT * FROM jobs WHERE state='queued' "
                          "ORDER BY COALESCE(json_extract(spec,'$.priority'),0) DESC, created, id").fetchall():
        spec = json.loads(row["spec"])
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
            elif any(host_free[w["host"]].get(k, 0) < n for k, n in spec["need"].items()):
                reason = "insufficient shared host resources"
            if reason:
                rejected.append({"worker": w["id"], "reason": reason})
                continue
            targets.append(Target(id=w["id"], host_id=w["host"], tier=Tier.LOCAL,
                                  capacity=host_free[w["host"]], labels=report["labels"]))
        job = Job(id=row["id"], kind=spec["handler"], owner=row["owner"], need=spec["need"],
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
                    encode(spec["need"]), now+limits.lease_seconds, now))
        db.execute("UPDATE jobs SET state='running',fence=?,updated=?,reason=? WHERE id=?",
                   (fence, now, grants[0].reason, row["id"]))
        busy.add(chosen["id"])
        for k, n in spec["need"].items():
            host_free[chosen["host"]][k] -= n
