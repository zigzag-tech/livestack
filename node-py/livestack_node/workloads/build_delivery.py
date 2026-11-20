"""Bounded build intent and usable-result estimates after hard admission.

These are placement facts, never authorization. Unknown throughput has a named
conservative budget; it is not reported as a measurement of zero transfer time.
"""
import json
import math
import statistics

from .model import WorkloadError, name

MAX_OUTPUT_BYTES = 8 * 1024**3
MAX_ROUTES = 16
HISTORY_ROWS = 4096
HISTORY_WINDOW = 20
HISTORY_AGE = 30 * 86400
ROUTE_AGE = 300


def validate_delivery(value):
    if not isinstance(value, dict) or set(value) - {'consumer_host', 'expected_output_bytes', 'routes'}:
        raise WorkloadError('invalid build delivery intent')
    if not {'consumer_host', 'expected_output_bytes'} <= set(value):
        raise WorkloadError('build delivery requires consumer host and output size')
    name(value['consumer_host'], 'delivery consumer host')
    size = value['expected_output_bytes']
    if type(size) is not int or not 0 <= size <= MAX_OUTPUT_BYTES:
        raise WorkloadError('delivery output size exceeds 8 GiB')
    routes = value.get('routes', [])
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        raise WorkloadError('delivery routes exceed count bound')
    hosts = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {'host', 'bytes_per_second', 'measured_at'}:
            raise WorkloadError('invalid measured delivery route')
        name(route['host'], 'delivery route host')
        if route['host'] in hosts:
            raise WorkloadError('duplicate delivery route host')
        hosts.add(route['host'])
        for key in ('bytes_per_second', 'measured_at'):
            number = route[key]
            if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                raise WorkloadError('invalid measured delivery route number')
    return dict(consumer_host=value['consumer_host'], expected_output_bytes=size,
                routes=[dict(route) for route in routes])


def history_key(handler, profile=None):
    # Existing handler-only observations retain their exact key. A profile never
    # learns from that mixed history or from another operation using the handler.
    return handler if profile is None else json.dumps([handler, profile], separators=(',', ':'))


def record_build(db, spec, host, attempt, result, outcome, now):
    if outcome != 'succeeded' or not spec.get('resource_profile'):
        return
    seconds = (result.get('resources') or {}).get('execution_seconds')
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        return
    db.execute('INSERT OR IGNORE INTO build_history(profile,host,attempt,seconds,at) VALUES(?,?,?,?,?)',
               (history_key(spec['handler'], spec['resource_profile']), host, attempt, seconds, now))
    # One global count/age bound, independent of caller-chosen profile cardinality.
    db.execute('DELETE FROM build_history WHERE at<? OR id NOT IN '
               '(SELECT id FROM build_history ORDER BY id DESC LIMIT ?)', (now-HISTORY_AGE, HISTORY_ROWS))


def read_history(db, now):
    result = {}
    for row in db.execute('SELECT profile,host,seconds FROM build_history WHERE at>=? '
                          'ORDER BY id DESC LIMIT ?', (now-HISTORY_AGE, HISTORY_ROWS)):
        values = result.setdefault((row['profile'], row['host']), [])
        if len(values) < HISTORY_WINDOW:
            values.append(row['seconds'])
    return result


def rank_targets(targets, reports, spec, history, now):
    """Return feasible targets ordered by delivery time, plus bounded evidence.

    The ordinary fleet scheduler still grants resources and applies deadlines.
    Replacing its candidate order alone would let its own tie breaker override
    delivery, so the caller admits the best feasible target from this ranking.
    """
    delivery = spec.get('delivery')
    if delivery is None and not spec.get('resource_profile'):
        return targets, None
    profile = history_key(spec['handler'], spec.get('resource_profile'))
    known = [v for (p, _), values in history.items() if p == profile for v in values]
    fallback = max(known) if known else spec['estimate_seconds']
    if delivery is None:
        # A profile without delivery intent (a release compile whose result goes to the
        # shared store either way) ranks by measured build time alone. Until some candidate
        # host has been measured there is nothing to rank: the scheduler decides as before.
        # An unmeasured host is charged the slowest known build, so it is used when the
        # measured hosts cannot take the job, and is measured from then on.
        if not any(history.get((profile, t.host_id)) for t in targets):
            return targets, None
        delivery = dict(consumer_host=None, expected_output_bytes=None, routes=[])
    routes = {r['host']: r for r in delivery['routes'] if 0 <= now-r['measured_at'] <= ROUTE_AGE}
    ranked = []
    for target in targets:
        samples = history.get((profile, target.host_id), [])
        build = statistics.median(samples) if samples else fallback
        local = (delivery['consumer_host'] is not None and target.host_id == delivery['consumer_host'] and
                 reports[target.id].get('local_artifacts') is True)
        route = routes.get(target.host_id)
        if delivery['expected_output_bytes'] is None:
            transfer, basis = 0, 'no-delivery-intent'
        elif local:
            transfer, basis = 0, 'same-host-verified-handoff'
        elif delivery['expected_output_bytes'] == 0:
            transfer, basis = 0, 'no-output-bytes'
        elif route is not None:
            transfer = delivery['expected_output_bytes']/route['bytes_per_second']
            basis = 'caller-measured-throughput'
        else:
            transfer, basis = fallback, 'unknown-route-conservative-build-budget'
        evidence = dict(worker=target.id, host=target.host_id, build_seconds=build,
                        build_basis='profile-host-history' if samples else 'conservative-profile-or-request',
                        transfer_seconds=transfer, transfer_basis=basis, usable_seconds=build+transfer)
        ranked.append((evidence['usable_seconds'], target.id, target, evidence))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked], [row[3] for row in ranked]
