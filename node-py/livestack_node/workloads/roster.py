"""Read-only fleet roster: the workers as the AUTHORITY sees them.

One answer to "who is on this fleet, are they alive, what are they doing, and why
is one not taking work" that merges what the operator CONFIGURED (the worker
principals the running authority holds) with what has REGISTERED (the workers
table) and what is RUNNING (attempts). Secrets never appear: only principal ids,
hosts and the claim_enabled flag leave this module.

Disagreements are the point. A configured worker that never registered, or went
silent; a registered worker nobody configured; a worker advertising a strict
subset of a same-host peer's handlers; a handler-release label that differs
between same-host peers; a worker whose last handler activation failed. Each is
named, not inferred from a missing row.
"""
import json

QUEUE_LIMIT = 50
HOLDERS_SHOWN = 6
BYPASS_ROWS = 5000
RESOURCES = (('cpu', 'cpu'), ('memory_bytes', 'memory'), ('disk_bytes', 'disk'))


def _age(now, seen):
    return max(0.0, round(now - seen, 1))


def _reasons(entry, fresh_seconds):
    """Why a worker would not take a new job now; empty means it would."""
    if not entry['registered']:
        return ['never_registered']
    reasons = []
    if not entry['connected']:
        return [f"silent_{int(entry['last_seen_age_s'])}s_over_{int(fresh_seconds)}s_freshness"]
    if entry['claim_enabled'] is False:
        reasons.append('draining: claim_enabled=false')
    if not entry['ready']:
        reasons.append('worker_reports_not_ready')
    capacity, available = entry['capacity'] or {}, entry['available'] or {}
    for key, label in (('cpu', 'cpu'), ('memory_bytes', 'memory'), ('disk_bytes', 'disk')):
        if capacity.get(key, 0) > 0 and available.get(key, 0) <= 0:
            reasons.append(f'{label}_exhausted: available 0 of {capacity[key]:g}')
    host = entry['host_pressure'] or {}
    if host.get('memory_available_bytes') is not None and host.get('memory_reserve_bytes') is not None \
            and host['memory_available_bytes'] < host['memory_reserve_bytes']:
        reasons.append('memory_below_host_reserve')
    return reasons


def _host_pressure(report):
    host = report.get('host') or {}
    cpu = ((host.get('psi') or {}).get('cpu') or {})
    return {key: host.get(key) for key in ('memory_available_bytes', 'memory_reserve_bytes',
                                           'memory_total_bytes') if key in host} | (
        {'cpu_psi_full_avg60': cpu.get('full_avg60'), 'cpu_psi_some_avg60': cpu.get('some_avg60')}
        if cpu else {})


def _disagreements(entries):
    found = []
    for entry in entries:
        name = entry['id']
        if entry['configured'] and not entry['registered']:
            found.append(dict(worker=name, kind='configured_never_registered',
                              detail='principal is configured on the authority but this worker has never reported'))
        elif entry['configured'] and not entry['connected']:
            found.append(dict(worker=name, kind='configured_silent',
                              detail=f"last report {entry['last_seen_age_s']}s ago (freshness window exceeded)"))
        if entry['registered'] and not entry['configured'] and not entry['remote']:
            found.append(dict(worker=name, kind='registered_not_configured',
                              detail='worker has a registration row but the authority holds no principal for it'))
        if entry['activation_failures']:
            failure = entry['activation_failures'][0]
            found.append(dict(worker=name, kind='handler_activation_failed',
                              detail=f"generation {failure.get('generation')}: {failure.get('reason')}"))
    live = [e for e in entries if e['connected']]
    by_host = {}
    for entry in live:
        by_host.setdefault(entry['host'], []).append(entry)
    for host, peers in sorted(by_host.items()):
        for entry in peers:
            mine = set(entry['handlers'])
            for peer in peers:
                theirs = set(peer['handlers'])
                if peer is not entry and mine < theirs:
                    found.append(dict(worker=entry['id'], kind='fewer_handlers_than_peer',
                                      detail=f"host {host}: lacks {sorted(theirs - mine)} that {peer['id']} serves"))
                    break
        # Release skew is only a disagreement between workers serving the SAME handler set
        # (same role); a compilation worker and a stager legitimately differ.
        roles = {}
        for entry in peers:
            roles.setdefault(tuple(sorted(entry['handlers'])), []).append(entry)
        for group in roles.values():
            keys = sorted({k for e in group for k in e['labels'] if k.endswith('handler_release')})
            for key in keys:
                values = {e['id']: e['labels'][key] for e in group if key in e['labels']}
                if len(values) > 1 and len(set(values.values())) > 1:
                    found.append(dict(worker=sorted(values)[0], kind='handler_release_skew',
                                      detail=f'host {host}: {key} differs across same-role peers: '
                                             + ', '.join(f'{w}={v[:12]}' for w, v in sorted(values.items()))))
    return found


def _gib(value):
    return f'{value / 2**30:.1f}GiB' if value >= 2**20 else f'{value:g}'


def _amount(key, value):
    return _gib(value) if key.endswith('_bytes') else f'{value:g}'


def _verdict(entry, spec, admit, host_held):
    """Why this worker has not taken the job: [] means nothing the roster can see blocks it.

    The resource arithmetic mirrors placement (a worker's observed free, less what every
    ACTIVE attempt on the same host has reserved) closely enough to NAME the holders; the
    authority's own last verdict stays alongside it as `reason`."""
    reasons = list(entry['ineligible_reasons'])
    if entry['running'] and not any(r.startswith('draining') for r in reasons):
        held = entry['running'][0]
        reasons.append(f"busy: holds attempt for job {held['job_id'][:8]} ({held['handler']})")
    for key, want in sorted((spec.get('selector') or {}).items()):
        if entry['labels'].get(key) != want:
            reasons.append(f"selector: label {key} is {entry['labels'].get(key)!r}, job requires {want!r}")
    capacity, available = entry['capacity'] or {}, entry['available'] or {}
    for key, label in RESOURCES:
        need = admit.get(key)
        if not need or key not in capacity:
            continue
        observed = min(capacity[key], available.get(key, capacity[key]))
        free = max(0, observed - host_held.get('total', {}).get(key, 0))
        if free < need:
            reasons.append(f"{label}: needs {_amount(key, need)}, {_amount(key, free)} free "
                           f"({_amount(key, observed)} observed less {_amount(key, host_held.get('total', {}).get(key, 0))} "
                           f"reserved by running attempts on host {entry['host']})")
    return reasons


def _queue(db, now, entries, limit=QUEUE_LIMIT):
    """Queued jobs and, per worker serving the handler, the concrete reason it has not claimed.

    Placement already records its own last verdict on the job (`reason`); that is carried
    verbatim. Beside it: how long the job has waited, what the same hosts have started
    since it was submitted (a large job starved by smaller ones shows as `started_since`
    climbing while its reason stays a resource refusal), and who holds the capacity."""
    jobs = db.execute("SELECT id, owner, created, reason, spec FROM jobs WHERE state='queued' "
                      "ORDER BY COALESCE(json_extract(spec,'$.priority'),0) DESC, created, id LIMIT ?",
                      (limit,)).fetchall()
    if not jobs:
        return []
    active = db.execute("SELECT a.worker, a.host, a.job, a.need, j.spec FROM attempts a JOIN jobs j ON j.id=a.job "
                        "WHERE a.state IN ('running','cleanup')").fetchall()
    held = {}
    for row in active:
        host = held.setdefault(row['host'], dict(total={}, holders=[]))
        need = json.loads(row['need'])
        for key, value in need.items():
            host['total'][key] = host['total'].get(key, 0) + value
        host['holders'].append(dict(job_id=row['job'], worker=row['worker'],
                                    handler=json.loads(row['spec']).get('handler'), admit=need))
    started = db.execute("SELECT host, created FROM attempts WHERE created>=? ORDER BY created DESC LIMIT ?",
                         (min(job['created'] for job in jobs), BYPASS_ROWS)).fetchall()
    queue = []
    for job in jobs:
        spec = json.loads(job['spec'])
        admit = spec.get('admit') or spec.get('need') or {}
        serving = [e for e in entries if spec.get('handler') in e['handlers'] and e['registered']]
        hosts = {e['host'] for e in serving}
        reason = job['reason']
        try:
            reason = json.loads(reason) if reason else None
        except ValueError:
            pass
        workers = []
        for entry in serving:
            host_held = held.get(entry['host'], dict(total={}, holders=[]))
            workers.append(dict(
                worker=entry['id'], host=entry['host'], state=entry['state'],
                blocked_by=_verdict(entry, spec, admit, host_held),
                host_holders=sorted(host_held['holders'], key=lambda h: -sum(h['admit'].values()))[:HOLDERS_SHOWN]))
        queue.append(dict(
            job_id=job['id'], handler=spec.get('handler'), owner=job['owner'],
            age_s=_age(now, job['created']), priority=spec.get('priority', 0), admit=admit,
            reason=reason, serving_workers=len(serving),
            started_since=sum(1 for row in started if row['host'] in hosts and row['created'] > job['created']),
            workers=workers))
    return queue


def build(store, principals):
    """The roster. `principals` is the authority's live set (server.principals)."""
    now = store.clock()
    fresh = store.limits.fresh_seconds
    with store.transaction() as db:
        rows = db.execute('SELECT id, host, boot, report, seen, ready FROM workers ORDER BY id LIMIT ?',
                          (store.limits.workers,)).fetchall()
        running = db.execute(
            "SELECT a.worker, a.job, a.created, a.expires, j.spec FROM attempts a JOIN jobs j ON a.job=j.id "
            "WHERE a.state='running' ORDER BY a.created").fetchall()
    configured = {}
    for principal in principals:
        if principal.role == 'worker':
            configured.setdefault(principal.worker, principal)
    runs = {}
    for row in running:
        runs.setdefault(row['worker'], []).append(dict(
            job_id=row['job'], handler=json.loads(row['spec']).get('handler'),
            running_for_s=_age(now, row['created']), lease_remaining_s=max(0.0, round(row['expires'] - now, 1))))
    registered = {row['id']: row for row in rows}
    entries = []
    for worker in sorted(set(configured) | set(registered)):
        principal, row = configured.get(worker), registered.get(worker)
        report = json.loads(row['report']) if row else {}
        age = _age(now, row['seen']) if row else None
        connected = bool(row) and now - row['seen'] <= fresh
        inventory = report.get('handler_inventory') or {}
        entry = dict(
            id=worker, host=(principal.host if principal else row['host']),
            configured=principal is not None, registered=row is not None,
            remote=bool(row) and row['host'] in store.remote_hosts.values(),
            connected=connected, last_seen_age_s=age,
            claim_enabled=principal.claim_enabled if principal else None,
            ready=bool(row['ready']) if row else False,
            state='offline' if not connected else ('running' if runs.get(worker) else 'idle'),
            running=runs.get(worker, []),
            handlers=report.get('handlers', []),
            labels=report.get('labels', {}),
            capacity=report.get('capacity'), available=report.get('available'),
            host_pressure=_host_pressure(report),
            handler_generation=inventory.get('generation'),
            handler_releases=[dict(handler=r['handler_id'], release=r['release_digest'][:16])
                              for r in inventory.get('releases', [])],
            activation_failures=report.get('handler_activation_failures', []),
        )
        reasons = _reasons(entry, fresh)
        entry['eligible'] = not reasons and connected
        entry['ineligible_reasons'] = reasons
        entries.append(entry)
    with store.transaction() as db:
        queue = _queue(db, now, entries)
    return dict(now=round(now, 3), fresh_seconds=fresh, workers=entries, disagreements=_disagreements(entries),
                queue=queue)
