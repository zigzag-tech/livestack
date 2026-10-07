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
    return dict(now=round(now, 3), fresh_seconds=fresh, workers=entries, disagreements=_disagreements(entries))
