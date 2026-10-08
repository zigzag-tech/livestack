"""Adapt durable worker claims to Harmony's existing pure fleet scheduler.

Called only inside the authority's immediate transaction. Assign one attempt per
worker initially; all environments on the same physical host share claims.
"""
import json
import logging
import uuid

from ..fleet_scheduler import Admit, FleetState, Job, Sla, Target, Tier, schedule
from ..ledger import new_decision_id
from .model import AVOID_LABEL_SIGNATURE, AVOID_LABEL_WORKER, WorkloadError, encode, failure_signature
from .decision_records import admission_record

# How long after an infrastructure failure a job refuses the worker that
# produced it, while some other worker could ever run it. Fixed and short
# relative to a recovery: a lone worker that heals is used again after this.
AVOID_SECONDS = 1800

# Measured-host memory (openspec/changes/host-memory-ledger). A host whose
# workers report a `host` block is charged learned claims, not admit vectors,
# for memory; hosts without one are placed exactly as before.
MEMORY = "memory_bytes"
# A handler's claim is the max recorded peak over its last N SUCCEEDED
# attempts: it follows a handler that grew or shrank within a day of traffic.
# Only successes teach: an attempt that died in preparation peaks low, and on
# 2026-10-02 twenty of them in a row would have taught the e2e handler 3.8 GiB
# while its completed runs reach 10 GiB.
LEARNED_WINDOW = 20
# Admission stops on a host that is already stalling on memory. avg60, not
# avg10, so one burst does not flap admission. 16 MiB/s of swap-in is ~4k
# pages/s: far above an idle server faulting back in (zz-joe at rest: <10/s).
# IO pressure is not gated: an attempt's own image build saturates IO legitimately.
PRESSURE_MEMORY_FULL_AVG60 = 5.0
PRESSURE_SWAP_IN_BYTES_PER_SECOND = 16 * 1024**2


def _gib(n):
    return f"{n / 1024**3:.1f}"


# Seconds a job may wait on the SAME refusal code from every candidate worker before one
# event announces it (limits.stall_report_seconds); remembered per (job, code), bounded.
_STALL_REPORTED = {}


def _disk_refusal(worker_id, report, host_free, worker_free, need):
    """A named disk refusal (code, reason, figures) when the worker's offered disk cannot hold
    `need`, else None. Replaces the generic "insufficient shared host resources" for disk."""
    free = min(host_free, worker_free)
    if need is None or free >= need:
        return None
    capacity = report["capacity"].get("disk_bytes", 0)
    gone = report.get("disk_unavailable")
    if gone:
        reason = (f"worker {worker_id}: disk offered {_gib(gone['offered_bytes'])} of {_gib(gone['capacity_bytes'])} GiB "
                  f"(free {_gib(gone['free_bytes'])} GiB < reserve {_gib(gone['reserve_bytes'])} GiB, "
                  f"{gone['reason']})")
        return "disk_reserve", reason, dict(gone, need_bytes=need)
    figures = dict(need_bytes=need, free_bytes=free, capacity_bytes=capacity)
    return "disk_need", (f"worker {worker_id}: disk need {_gib(need)} GiB > free {_gib(free)} GiB "
                         f"of {_gib(capacity)} GiB offered"), figures


def _stall_event(row, rejected, now, limits):
    """One logged event when every candidate worker refuses a job for the same coded reason for
    longer than limits.stall_report_seconds (the 20-minute stager stall was discovered, not announced)."""
    codes = {item.get("reason_code") for item in rejected}
    seconds = getattr(limits, "stall_report_seconds", None)
    if not rejected or len(codes) != 1 or None in codes or not seconds or now-row["created"] < seconds:
        return
    key = (row["id"], next(iter(codes)))
    if key in _STALL_REPORTED:
        return
    if len(_STALL_REPORTED) >= 1024:
        _STALL_REPORTED.clear()
    _STALL_REPORTED[key] = now
    logging.warning("placement_stalled: job %s waited %.0fs; every candidate refuses with %s: %s",
                    row["id"], now-row["created"], key[1], "; ".join(i["reason"] for i in rejected[:4])[:512])


def _learned_peak(db, handler, cache):
    """Prefer the non-reclaimable peak the worker samples; fall back to the
    cgroup memory.peak (page cache included, so conservative) only while no
    succeeded attempt of the handler carries the newer figure.

    Reads the bounded resource history (resource_history.py), the one learned number
    shared with the declaration audit."""
    if handler not in cache:
        cache[handler] = None
        for dimension in ("memory_nonreclaimable_peak", "memory_peak"):
            peaks = [r[0] for r in db.execute(
                "SELECT value FROM resource_history WHERE handler=? AND dimension=? AND outcome='succeeded' "
                "ORDER BY at DESC,id DESC LIMIT ?", (handler, dimension, LEARNED_WINDOW))]
            if peaks:
                cache[handler] = max(peaks)
                break
    return cache[handler]


def _memory_claim(db, spec, cache):
    """What an attempt of this job will hold: its handler's learned peak within
    [admit, need], or `need` (its cgroup MemoryMax) until a peak is recorded.
    None when the job declares no memory."""
    need = spec["need"].get(MEMORY)
    if need is None:
        return None
    admit = (spec.get("admit") or spec["need"]).get(MEMORY, need)
    learned = _learned_peak(db, spec["handler"], cache)
    return need if learned is None else min(need, max(admit, learned))


def _pressure(view):
    memory = (view.get("psi") or {}).get("memory") or {}
    full = memory.get("full_avg60")
    if full is not None and full >= PRESSURE_MEMORY_FULL_AVG60:
        return f"host memory pressure: memory full avg60 {full:.1f}% (limit {PRESSURE_MEMORY_FULL_AVG60:g}%)"
    swap = view.get("swap_in_bytes_per_second")
    if swap is not None and swap >= PRESSURE_SWAP_IN_BYTES_PER_SECOND:
        return (f"host memory pressure: swap-in {swap / 1024**2:.1f} MiB/s "
                f"(limit {PRESSURE_SWAP_IN_BYTES_PER_SECOND / 1024**2:g} MiB/s)")
    return None


def _compilation_refusal(policy, worker, spec, now):
    if policy is None or not policy.required(spec['handler']):
        return None
    try:
        policy.authorize(worker['host'], spec['handler'], now)
    except WorkloadError as error:
        return str(error)
    return None


def _release_compatible(report, spec):
    required = spec.get('handler_release')
    if required is None:
        return True
    inventory = report.get('handler_inventory')
    if not isinstance(inventory, dict):
        return False
    return any(item.get('handler_id') == required['handler_id'] and
               item.get('release_digest') == required['release_digest'] and
               item.get('execution_contract') == required['execution_contract']
               for item in inventory.get('releases', []))


def _avoided(db, row, now):
    """{worker: (signature, where)} this queued job must not return to yet."""
    out = {}
    if now - row["updated"] < AVOID_SECONDS:
        for a in db.execute("SELECT worker,fence,result FROM attempts WHERE job=? "
                            "AND result IS NOT NULL ORDER BY fence", (row["id"],)):
            sig = failure_signature(json.loads(a["result"]))
            if sig:
                out[a["worker"]] = (sig, f"attempt {a['fence']}")
    labels = json.loads(row["labels"] or "{}")
    worker, sig = labels.get(AVOID_LABEL_WORKER), labels.get(AVOID_LABEL_SIGNATURE)
    if worker and sig and now - row["created"] < AVOID_SECONDS:
        out.setdefault(worker, (sig, "the previous job's last attempt"))
    return out


def place(db, now, limits, principals=None, compilation_policy=None, *, only_job_id=None,
          emitter_id=None):
    decision_records = []
    draining = {p.worker for p in (principals or {}).values()
                if getattr(p, 'role', None) == 'worker' and not p.claim_enabled}
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
        "SELECT a.*, j.spec AS job_spec FROM attempts a JOIN workers w ON w.id=a.worker "
        "JOIN jobs j ON j.id=a.job "
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
    # Memory on a measured host: the freshest `host` block is the machine's view.
    # Its MemAvailable already contains every tenant's current use, so only the
    # unrealised part of each claim is still to come. Model servers are charged
    # their largest outstanding transient (not the sum: independent servers'
    # load spikes summed exceed zz-joe's RAM, and coincident spikes are what the
    # pressure gate catches).
    views, seen = {}, {}
    for w in workers:
        view = reports[w["id"]].get("host")
        if view is not None and w["seen"] >= seen.get(w["host"], float("-inf")):
            views[w["host"]], seen[w["host"]] = view, w["seen"]
    learned = {}
    pressure, memory_terms = {}, {}
    for h, view in views.items():
        reason = _pressure(view)
        if reason:
            pressure[h] = reason
        pending = 0
        for a in active:
            if a["host"] != h:
                continue
            claim = _memory_claim(db, json.loads(a["job_spec"]), learned)
            if claim is not None:
                pending += max(0, claim - view["attempts"].get(a["id"], 0))
        # A service whose units are all resident cannot load again, so its
        # spike is not outstanding. Only an explicit True clears it: a failed
        # residence read (null) is charged, never read as "no spike".
        services = max((max(0, s["peak_bytes"] - s["current_bytes"]) for s in view["services"].values()
                        if s.get("resident") is not True), default=0)
        memory_terms[h] = dict(available=view["memory_available_bytes"], reserve=view["memory_reserve_bytes"],
                               attempts=pending, services=services, admitted=0)
        host_free.setdefault(h, {})[MEMORY] = max(0, view["memory_available_bytes"]
                                                  - view["memory_reserve_bytes"] - pending - services)
        for w in workers:
            if w["host"] == h:
                # The configured capacity (or the measured MemTotal) stays a
                # per-identity ceiling; the measured host decides the rest.
                worker_free[w["id"]][MEMORY] = reports[w["id"]]["capacity"].get(MEMORY, 0)
    # Priority is caller intent, while Harmony still owns capability/resource
    # admission and the final worker choice. Legacy persisted specs omit the
    # field and retain their original priority-zero FIFO behavior.
    fresh = None  # every fresh worker regardless of readiness, loaded on first need
    queue_sql = "SELECT * FROM jobs WHERE state='queued'"
    queue_params = ()
    if only_job_id is not None:
        queue_sql += ' AND id=?'
        queue_params = (only_job_id,)
    queue_sql = queue_sql.replace('SELECT * FROM jobs',
        'SELECT j.*,e.purpose AS environment_purpose,e.profile AS environment_profile,'
        'e.state AS environment_state,e.generation AS environment_generation,'
        'e.writer_job AS environment_writer_job,e.writer_attempt AS environment_writer_attempt,'
        'e.affinity_started AS environment_affinity_started FROM jobs j '
        'LEFT JOIN task_environments e ON e.handle=j.environment_handle')
    queue_sql = queue_sql.replace('WHERE state=', 'WHERE j.state=').replace(' AND id=?', ' AND j.id=?')
    queued = db.execute(queue_sql+" ORDER BY COALESCE(json_extract(j.spec,'$.priority'),0) DESC, j.created, j.id",
                        queue_params).fetchall()
    handles = sorted({row['environment_handle'] for row in queued if row['environment_handle']})
    replicas_by_handle = {}
    if handles:
        placeholders = ','.join('?' for _ in handles)
        replicas = db.execute(f'SELECT handle,host,profile,compatibility,generation,state,bytes_used,last_used,seen '
                              f'FROM task_environment_replicas WHERE handle IN ({placeholders}) AND seen>? '
                              'ORDER BY handle,last_used DESC,host LIMIT ?',
                              (*handles, now-limits.fresh_seconds, min(2048, 2*len(handles)))).fetchall()
        for replica in replicas:
            replicas_by_handle.setdefault(replica['handle'], []).append(replica)
    active_environment_writers = set()
    for row in queued:
        spec = json.loads(row["spec"])
        environment_handle = row['environment_handle']
        environment_profile = row['environment_profile']
        if environment_handle and environment_profile is None:
            db.execute("UPDATE jobs SET reason='environment_not_found' WHERE id=?", (row['id'],))
            continue
        if environment_handle and (row['environment_writer_job'] is not None or
                                   environment_handle in active_environment_writers):
            db.execute("UPDATE jobs SET reason='environment_busy' WHERE id=?", (row['id'],))
            continue
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
        claim = _memory_claim(db, spec, learned) if views else None
        targets = []
        rejected = []
        handler_workers = [w for w in workers if spec["handler"] in reports[w["id"]]["handlers"]]
        compatible = [w for w in handler_workers if _release_compatible(reports[w['id']], spec)]
        avoided = _avoided(db, row, now)
        matching_hosts = set()
        warm_wait_hosts = set()
        if environment_handle:
            for replica in replicas_by_handle.get(environment_handle, []):
                if (replica['profile'] != environment_profile or replica['compatibility'] is None or
                        replica['state'] != 'parked' or replica['generation'] != row['environment_generation']):
                    continue
                for worker in workers:
                    profile_compatibility = reports[worker['id']].get('environment_profiles', {}).get(environment_profile)
                    if worker['host'] == replica['host'] and profile_compatibility == replica['compatibility']:
                        matching_hosts.add(worker['host'])
                        break
        if avoided:
            if fresh is None:
                fresh = [(w, json.loads(w["report"])) for w in db.execute(
                    "SELECT * FROM workers WHERE seen>?", (now-limits.fresh_seconds,))]
            # Momentary load is ignored: another worker that merely is busy
            # still counts, so the job waits for it. Only a roster with no
            # other worker able to run the job at all lets it go back.
            alternative = any(
                w["id"] not in avoided and w["id"] not in draining and spec["handler"] in r["handlers"]
                and _release_compatible(r, spec)
                and all(r["labels"].get(k) == v for k, v in spec["selector"].items())
                and all(r["capacity"].get(k, 0) >= n for k, n in admit.items())
                and _compilation_refusal(compilation_policy, w, spec, now) is None
                for w, r in fresh)
        for w in compatible:
            report = reports[w["id"]]
            profile_compatibility = report.get('environment_profiles', {}).get(environment_profile) \
                if environment_handle else None
            replica_compatibility = next((r['compatibility'] for r in replicas_by_handle.get(environment_handle, [])
                if r['host'] == w['host'] and r['profile'] == environment_profile and r['state'] == 'parked' and
                   r['generation'] == row['environment_generation']), None) \
                if environment_handle else None
            if (environment_handle and w['host'] in matching_hosts and
                    profile_compatibility == replica_compatibility and w['id'] not in draining and
                    _compilation_refusal(compilation_policy, w, spec, now) is None and
                    all(report['labels'].get(k) == v for k, v in spec['selector'].items()) and
                    all(report['capacity'].get(k, 0) >= n for k, n in admit.items()) and
                    not avoided.get(w['id'])):
                warm_wait_hosts.add(w['host'])
            if w['id'] in draining:
                rejected.append({'worker': w['id'], 'reason': 'worker_draining'})
                continue
            if environment_handle and profile_compatibility is None:
                rejected.append({'worker': w['id'], 'reason': 'environment_profile_not_installed'})
                continue
            reason = _compilation_refusal(compilation_policy, w, spec, now)
            if reason:
                rejected.append({"worker": w['id'], "reason": reason})
                continue
            if avoided.get(w["id"]) and alternative:
                sig, where = avoided[w["id"]]
                reason = f"avoiding {w['id']}: same failure signature {sig} on {where}"
            elif w["id"] in busy:
                reason = "worker holds an active attempt or cleanup"
            elif any(report["labels"].get(k) != v for k, v in spec["selector"].items()):
                reason = "required capability absent"
            elif w["host"] in pressure:
                reason = pressure[w["host"]]
            elif (w["host"] in views and claim is not None and
                  min(host_free[w["host"]][MEMORY], worker_free[w["id"]].get(MEMORY, 0)) < claim):
                t = memory_terms[w["host"]]
                reason = (f"insufficient host memory: claim {_gib(claim)} GiB > free "
                          f"{_gib(min(host_free[w['host']][MEMORY], worker_free[w['id']].get(MEMORY, 0)))} GiB "
                          f"(available {_gib(t['available'])}, reserve {_gib(t['reserve'])}, "
                          f"running attempts {_gib(t['attempts'])}, model servers {_gib(t['services'])}, "
                          f"admitted now {_gib(t['admitted'])})")
            elif (disk := _disk_refusal(w["id"], report, host_free[w["host"]].get("disk_bytes", 0),
                                        worker_free[w["id"]].get("disk_bytes", 0), admit.get("disk_bytes"))):
                code, reason, figures = disk
                rejected.append({"worker": w["id"], "reason": reason, "reason_code": code, "figures": figures})
                continue
            elif any(min(host_free[w["host"]].get(k, 0), worker_free[w["id"]].get(k, 0)) < n
                     for k, n in admit.items() if not (k == MEMORY and w["host"] in views and claim is not None)):
                # One reason for both bounds: a caller can act on neither
                # differently, and the distinct figures are already in the
                # worker report the refusal is recorded against.
                reason = "insufficient shared host resources"
            if reason:
                rejected.append({"worker": w["id"], "reason": reason})
                continue
            targets.append(Target(id=w["id"], host_id=w["host"], tier=Tier.LOCAL,
                                  capacity=host_free[w["host"]], labels=report["labels"]))
        affinity_started = row['environment_affinity_started']
        if environment_handle:
            warm_targets = [target for target in targets if target.host_id in matching_hosts]
            cold_targets = [target for target in targets if target.host_id not in matching_hosts]
            if warm_targets:
                db.execute('UPDATE task_environments SET affinity_started=NULL WHERE handle=?', (environment_handle,))
            elif cold_targets and warm_wait_hosts and limits.environment_affinity_seconds > 0:
                if affinity_started is None:
                    affinity_started = now
                    db.execute('UPDATE task_environments SET affinity_started=? WHERE handle=?',
                               (affinity_started, environment_handle))
                if now-affinity_started < limits.environment_affinity_seconds:
                    db.execute("UPDATE jobs SET reason='environment_affinity_wait' WHERE id=?", (row['id'],))
                    continue
            elif not warm_wait_hosts:
                db.execute('UPDATE task_environments SET affinity_started=NULL WHERE handle=?', (environment_handle,))
        locality = spec['locality_host']
        if environment_handle and locality is None:
            preferred = next((target.host_id for target in targets if target.host_id in matching_hosts), None)
            if preferred is not None:
                locality = preferred
        job = Job(id=row["id"], kind=spec["handler"], owner=row["owner"], need=admit,
                  created_at=row["created"], sla=Sla.BATCH, deadline=spec["deadline"],
                  est_duration_s=spec["estimate_seconds"], selector=spec["selector"],
                  locality_host=locality)
        decision_id = new_decision_id(now)
        plan = schedule(FleetState(targets=tuple(targets), jobs=(job,), now=now),
                        decision_ids={job.id: decision_id})
        grants = plan.of(Admit)
        if not grants:
            if not workers:
                reason = "no fresh, reconciled worker"
            elif not compatible and spec.get('handler_release'):
                reason = (f"no fresh worker advertises release {spec['handler_release']['release_digest']} "
                          f"for handler {spec['handler']}")
            elif not compatible:
                reason = f"no fresh worker advertises handler {spec['handler']}"
            elif handler_workers and not compatible:
                reason = f"no fresh worker advertises handler {spec['handler']}"
            else:
                reason = encode(rejected or {"reason": "no target can meet deadline"})
                _stall_event(row, rejected, now, limits)
            db.execute("UPDATE jobs SET reason=? WHERE id=?", (reason[:8192], row["id"]))
            continue
        chosen = next(w for w in workers if w["id"] == grants[0].target_id)
        fence = row["fence"] + 1
        aid = uuid.uuid4().hex
        environment_generation = None
        compilation = None
        if compilation_policy is not None and compilation_policy.required(spec['handler']):
            compilation = encode(compilation_policy.authorize(chosen['host'], spec['handler'], now).receipt())
        if environment_handle:
            environment_generation = row['environment_generation'] + 1
            db.execute('UPDATE task_environments SET state=\'preparing\',generation=?,writer_job=?,writer_attempt=?,'
                       'affinity_started=NULL,updated=? WHERE handle=? AND writer_job IS NULL',
                       (environment_generation, row['id'], aid, now, environment_handle))
            if db.execute('SELECT changes()').fetchone()[0] != 1:
                raise WorkloadError('environment_writer_race', 409)
            active_environment_writers.add(environment_handle)
        pinned_release = spec.get('handler_release')
        db.execute("INSERT INTO attempts(id,job,worker,boot,host,fence,state,need,expires,created,compilation,environment_generation,handler_release,decision_id) "
                   "VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?,?)",
                   (aid, row["id"], chosen["id"], chosen["boot"], chosen["host"], fence,
                    encode(admit), now+limits.lease_seconds, now, compilation, environment_generation,
                    encode(pinned_release) if pinned_release is not None else None, decision_id))
        db.execute("UPDATE jobs SET state='running',fence=?,updated=?,reason=? WHERE id=?",
                   (fence, now, grants[0].reason, row["id"]))
        filtered = {item['worker']: item['reason'] for item in rejected}
        scheduler_candidates = {candidate['id'] for candidate in
                                plan.decisions[job.id]['candidates']}
        for worker in workers:
            if worker['id'] in scheduler_candidates or worker['id'] in filtered:
                continue
            report = reports[worker['id']]
            if worker['id'] in draining:
                reason = 'worker draining'
            elif spec['handler'] not in report['handlers']:
                reason = 'handler not advertised'
            elif worker not in compatible:
                reason = 'handler release incompatible'
            else:
                reason = 'excluded by placement prerequisites'
            filtered[worker['id']] = reason
        if emitter_id is not None:
            decision_records.append(admission_record(
                plan.decisions[job.id], now=now, emitter_id=emitter_id,
                kind=spec['handler'], owner=row['owner'], selector=spec['selector'],
                locality_host=locality, job_id=row['id'], attempt_id=aid,
                environment_handle=environment_handle,
                environment_generation=environment_generation,
                filtered_candidates=[
                    {'worker': worker['id'], 'host': worker['host'], 'reason': reason}
                    for worker in workers if (reason := filtered.get(worker['id']))
                ],
            ))
        busy.add(chosen["id"])
        running[row["owner"]] = running.get(row["owner"], 0) + 1
        measured = chosen["host"] in views and claim is not None
        for k, n in admit.items():
            if not (k == MEMORY and measured):
                host_free[chosen["host"]][k] -= n
        if measured:
            host_free[chosen["host"]][MEMORY] -= claim
            memory_terms[chosen["host"]]["admitted"] += claim
    return decision_records
