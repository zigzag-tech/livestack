"""Adapt durable worker claims to Harmony's existing pure fleet scheduler.

Called only inside the authority's immediate transaction. Assign one attempt per
worker initially; all environments on the same physical host share claims.
"""
import hashlib
import json
import logging
import uuid

from ..fleet_scheduler import Admit, FleetState, Job, Sla, Target, Tier, schedule
from ..ledger import new_decision_id
from .model import AVOID_LABEL_SIGNATURE, AVOID_LABEL_WORKER, WorkloadError, encode, failure_signature
from .decision_records import admission_record
from . import claims

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


_HOLD_CODES = frozenset(("resources_insufficient", "memory_insufficient", "disk_insufficient"))
_STARVED_REPORTED = {}


def _hold_for_starved(row, admit, claim, rejected, workers, reports, host_free, memory_terms, views, now, limits):
    """Reserve, on every host that refuses an aged job only for lack of free resources, what the job would take.

    Jobs placed later in this round then see the host as already charged, exactly as if the starved job had been
    admitted, so running attempts draining off the host make room for IT rather than for the next small job. Only
    a job the host could ever fit (its capacity covers `admit`) holds: a job bigger than the machine must not
    freeze it. The hold lasts one round; nothing persists."""
    if now-row["created"] < limits.starvation_seconds:
        return
    by_id = {w["id"]: w for w in workers}
    held = set()
    for item in rejected:
        w = by_id.get(item["worker"])
        if (w is None or item.get("code") not in _HOLD_CODES or w["host"] in held
                or any(reports[w["id"]]["capacity"].get(k, 0) < n for k, n in admit.items())):
            continue
        held.add(w["host"])
        measured = w["host"] in views and claim is not None
        for k, n in admit.items():
            if not (k == MEMORY and measured):
                host_free[w["host"]][k] -= n
        if measured:
            host_free[w["host"]][MEMORY] -= claim
            memory_terms[w["host"]]["admitted"] += claim
    if held and row["id"] not in _STARVED_REPORTED:
        if len(_STARVED_REPORTED) >= 1024:
            _STARVED_REPORTED.clear()
        _STARVED_REPORTED[row["id"]] = now
        logging.warning("placement_starved: job %s waited %.0fs for free resources on %s; holding them for it",
                        row["id"], now-row["created"], ", ".join(sorted(held)))


# Blocker codes (openspec typed-outcome-causes-and-blockers). A closed list: a reader tells "no worker advertises
# the handler" from "the one worker is busy" without parsing prose.
BLOCKER_CODES = frozenset((
    "worker_stale", "worker_draining", "worker_not_ready", "handler_not_advertised", "release_incompatible",
    "capability_absent", "memory_insufficient", "host_pressure", "principal_cap", "avoiding_failure_signature",
    "worker_busy", "environment_busy", "environment_affinity_wait", "environment_not_found",
    "environment_profile_not_installed", "compilation_refused", "disk_reserve", "disk_insufficient",
    "resources_insufficient", "deadline_unfit", "no_workers"))
PLACEMENT_BYTES = 4096
PLACEMENT_REFRESH_SECONDS = 60


def _wait(db, row, now, reason, blockers):
    """Say why a queued job was not placed this round.

    `reason` keeps today's human line, written only when its text changed (a steady wait is not an UPDATE per
    claim poll). `jobs.placement` carries the structured blockers; `since` is when this SET of blockers first
    appeared (worker, host, code: not the figures in `detail`, which move every round) and `evaluated` is
    refreshed at most once a minute, so a reader can compute how long the job has waited and why."""
    if reason is not None and row["reason"] != reason[:8192]:
        db.execute("UPDATE jobs SET reason=? WHERE id=?", (reason[:8192], row["id"]))
    blockers = [dict(worker=b.get("worker"), host=b.get("host"), code=b["code"], detail=str(b.get("detail", ""))[:160])
                for b in blockers]
    digest = hashlib.sha256(json.dumps([[b["worker"], b["host"], b["code"]] for b in blockers]).encode()).hexdigest()[:16]
    stored = json.loads(row["placement"]) if row["placement"] else None
    same = stored is not None and stored.get("digest") == digest
    if same and now - stored["evaluated"] < PLACEMENT_REFRESH_SECONDS:
        return
    document = dict(since=stored["since"] if same else now, evaluated=now, digest=digest,
                    blockers=blockers[:16], truncated=len(blockers) > 16)
    while len(encode(document, 1 << 20).encode()) > PLACEMENT_BYTES and document["blockers"]:
        document["blockers"] = document["blockers"][:-1]
        document["truncated"] = True
    db.execute("UPDATE jobs SET placement=? WHERE id=?", (encode(document, PLACEMENT_BYTES), row["id"]))


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
    draining = claims.draining(db, now, principals)  # claims store, else the principal's claim_enabled
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
    # A job queued longer than limits.starvation_seconds ranks ahead of priority: priority orders fresh work,
    # but must not let a stream of higher-priority small jobs refill a shared host forever (2026-10-09: two
    # release builds needing 16 GiB sat unclaimed 50 min behind priority-500000 e2e jobs while both release
    # workers were idle). Starved jobs also HOLD their host's resources (see _hold_for_starved).
    starved_before = now-limits.starvation_seconds
    queued = db.execute(queue_sql+" ORDER BY (j.created <= ?) DESC, "
                        "CASE WHEN j.created <= ? THEN 0 ELSE COALESCE(json_extract(j.spec,'$.priority'),0) END DESC, "
                        "j.created, j.id", (*queue_params, starved_before, starved_before)).fetchall()
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
            _wait(db, row, now, 'environment_not_found', [dict(code='environment_not_found')])
            continue
        if environment_handle and (row['environment_writer_job'] is not None or
                                   environment_handle in active_environment_writers):
            _wait(db, row, now, 'environment_busy', [dict(code='environment_busy')])
            continue
        cap = caps.get(row["owner"])
        if cap is not None and running.get(row["owner"], 0) >= cap:
            _wait(db, row, now, f"principal at max_running ({cap})",
                  [dict(code='principal_cap', detail=f"principal at max_running ({cap})")])
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
                rejected.append({'worker': w['id'], 'reason': 'worker_draining', 'code': 'worker_draining'})
                continue
            if environment_handle and profile_compatibility is None:
                rejected.append({'worker': w['id'], 'reason': 'environment_profile_not_installed',
                                 'code': 'environment_profile_not_installed'})
                continue
            reason = _compilation_refusal(compilation_policy, w, spec, now)
            if reason:
                rejected.append({"worker": w['id'], "reason": reason, "code": "compilation_refused"})
                continue
            code = None
            if avoided.get(w["id"]) and alternative:
                sig, where = avoided[w["id"]]
                reason, code = f"avoiding {w['id']}: same failure signature {sig} on {where}", "avoiding_failure_signature"
            elif w["id"] in busy:
                reason, code = "worker holds an active attempt or cleanup", "worker_busy"
            elif any(report["labels"].get(k) != v for k, v in spec["selector"].items()):
                reason, code = "required capability absent", "capability_absent"
            elif w["host"] in pressure:
                reason, code = pressure[w["host"]], "host_pressure"
            elif (w["host"] in views and claim is not None and
                  min(host_free[w["host"]][MEMORY], worker_free[w["id"]].get(MEMORY, 0)) < claim):
                t = memory_terms[w["host"]]
                reason = (f"insufficient host memory: claim {_gib(claim)} GiB > free "
                          f"{_gib(min(host_free[w['host']][MEMORY], worker_free[w['id']].get(MEMORY, 0)))} GiB "
                          f"(available {_gib(t['available'])}, reserve {_gib(t['reserve'])}, "
                          f"running attempts {_gib(t['attempts'])}, model servers {_gib(t['services'])}, "
                          f"admitted now {_gib(t['admitted'])})")
                code = "memory_insufficient"
            elif (disk := _disk_refusal(w["id"], report, host_free[w["host"]].get("disk_bytes", 0),
                                        worker_free[w["id"]].get("disk_bytes", 0), admit.get("disk_bytes"))):
                code, reason, figures = disk
                rejected.append({"worker": w["id"], "reason": reason, "reason_code": code, "figures": figures,
                                 "code": "disk_reserve" if code == "disk_reserve" else "disk_insufficient"})
                continue
            elif any(min(host_free[w["host"]].get(k, 0), worker_free[w["id"]].get(k, 0)) < n
                     for k, n in admit.items() if not (k == MEMORY and w["host"] in views and claim is not None)):
                # One reason for both bounds: a caller can act on neither
                # differently, and the distinct figures are already in the
                # worker report the refusal is recorded against.
                reason, code = "insufficient shared host resources", "resources_insufficient"
            if reason:
                rejected.append({"worker": w["id"], "reason": reason, "code": code})
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
                    _wait(db, row, now, 'environment_affinity_wait', [dict(code='environment_affinity_wait')])
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
            hosts = {w["id"]: w["host"] for w in workers}
            if not workers:
                reason = "no fresh, reconciled worker"
                blockers = [dict(code="no_workers", detail=reason)]
            elif not compatible and spec.get('handler_release'):
                reason = (f"no fresh worker advertises release {spec['handler_release']['release_digest']} "
                          f"for handler {spec['handler']}")
                blockers = [dict(code="release_incompatible" if handler_workers else "handler_not_advertised", detail=reason)]
            elif not compatible:
                reason = f"no fresh worker advertises handler {spec['handler']}"
                blockers = [dict(code="handler_not_advertised", detail=reason)]
            elif handler_workers and not compatible:
                reason = f"no fresh worker advertises handler {spec['handler']}"
                blockers = [dict(code="handler_not_advertised", detail=reason)]
            else:
                reason = encode(rejected or {"reason": "no target can meet deadline"})
                blockers = ([dict(worker=item["worker"], host=hosts.get(item["worker"]), code=item["code"],
                                  detail=item["reason"]) for item in rejected if item.get("code") in BLOCKER_CODES]
                            or [dict(code="deadline_unfit", detail="no target can meet deadline")])
                _stall_event(row, rejected, now, limits)
                _hold_for_starved(row, admit, claim, rejected, workers, reports, host_free, memory_terms,
                                  views, now, limits)
            _wait(db, row, now, reason, blockers)
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
        db.execute("UPDATE jobs SET state='running',placement=NULL,fence=?,updated=?,reason=? WHERE id=?",
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
