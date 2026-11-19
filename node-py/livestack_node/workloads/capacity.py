"""How many jobs of one handler the fleet could run at once NOW, judged by placement's own ledger.

`ready_workers` (store.handler_capacity) counts seats: ready, fresh, undrained workers serving the
handler. A seat is not a job. Several seats on one physical host share its memory, and a host whose
model server (a `host_services` tenant) holds most of its RAM at peak admits nothing however many
seats it offers. 2026-11-19: node-g offered 7 idle `benchday.e2e.full.v1` seats beside Strata's
~57 of 60 GB peak and took none of an 8-shard run, while ZZOPS sized its in-flight bound from the 7.

`admissible` replays admission over the same ledger `place` builds (placement._ledger): the same
charge (admission_charge), the same fit test (resource_refusal) and the same debit after each
admission, one simulated job at a time per host, so a second seat on a host sees the first one's
claim. Nothing is written.
"""
import json

from . import placement


def _template(db, handler):
    """The handler's most recent job spec: its declared `need`/`admit`/selector stand for the next one."""
    row = db.execute("SELECT id, spec FROM jobs WHERE json_extract(spec,'$.handler')=? "
                     "ORDER BY created DESC LIMIT 1", (handler,)).fetchone()
    return (None, None) if row is None else (row["id"], json.loads(row["spec"]))


def admissible(db, now, limits, principals, compilation_policy, handler, draining=()):
    """{admissible, basis, charge, hosts}. `admissible` = this handler's running attempts plus the jobs
    admissible now, summed over hosts; never above the seats `ready_workers` counts.

    Not modelled (job-specific, not capacity): queued jobs' priority holds, release pins, environment
    affinity and failure avoidance."""
    ledger = placement._ledger(db, now, limits, principals)
    draining = set(draining) | ledger.draining
    job_id, spec = _template(db, handler)
    if spec is None:
        # Placement charges a job only what it declares (no default vector exists), so with nothing
        # declared the only bound left is one attempt per worker.
        spec = dict(handler=handler, need={}, selector={})
        basis = (f"no {handler} job on record, so no declared need: placement charges an undeclared "
                 "dimension nothing, so each idle eligible seat counts as one job")
    else:
        basis = (f"placement ledger replay of the latest {handler} job's charge (job {job_id[:8]}); "
                 "per host: running attempts of the handler plus jobs admissible now, one at a time")
    _stored, admit, claim = placement.admission_charge(db, spec, ledger)
    running_handler = {a["worker"]: json.loads(a["job_spec"])["handler"]
                       for a in ledger.active if a["state"] == "running"}
    hosts = {}
    for w in ledger.workers:
        report = ledger.reports[w["id"]]
        if w["id"] in draining or handler not in report["handlers"]:
            continue  # not a seat: ready_workers does not count it either
        entry = hosts.setdefault(w["host"], dict(host=w["host"], seats=0, running=0, admissible_now=0, refused={}))
        entry["seats"] += 1
        if w["id"] in ledger.stale_capacity:
            code = "host_capacity_stale"
        elif any(value > report.get("execution_limit", {}).get(key, float("inf"))
                 for key, value in spec["need"].items()):
            code = "execution_budget_exceeded"
        elif placement._compilation_refusal(compilation_policy, w, spec, now):
            code = "compilation_refused"
        elif any(report["labels"].get(k) != v for k, v in spec["selector"].items()):
            code = "capability_absent"
        elif w["id"] in ledger.busy:
            if running_handler.get(w["id"]) == handler:
                entry["running"] += 1
                continue
            code = "worker_busy"
        elif (refusal := placement.resource_refusal(w, report, admit, claim, ledger)):
            code = refusal["code"]
        else:
            entry["admissible_now"] += 1
            placement.debit(w["host"], admit, claim, ledger)
            ledger.busy.add(w["id"])
            continue
        entry["refused"][code] = entry["refused"].get(code, 0) + 1
    out = []
    for entry in sorted(hosts.values(), key=lambda e: e["host"]):
        entry["admissible"] = entry["running"] + entry["admissible_now"]
        out.append(entry)
    return dict(admissible=sum(e["admissible"] for e in out), basis=basis,
                charge=dict(admit=admit, memory_claim_bytes=claim), hosts=out)
