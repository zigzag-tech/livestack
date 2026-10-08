"""Bounded per-handler resource history and the declaration audit.

Derived data (openspec/changes/measured-resource-declarations): one row per attempt and
dimension for succeeded and resource-limited attempts. Infrastructure outcomes are not
evidence. A killed attempt's peak is a lower bound on need, the most informative sample.
Everything here takes an open transaction; reads are ONE statement however many handlers
exist (Benchday rule 14 analogue).
"""
import json
import math

WINDOW = 50
MIN_SAMPLES = 5
MAX_AGE_SECONDS = 30*86400
SUGGEST_MARGIN = 1.15  # named in the warning text; never applied automatically
# dimension -> the `need` key it is audited against (None: no declared counterpart)
DIMENSIONS = {'memory_nonreclaimable_peak': 'memory_bytes', 'memory_peak': 'memory_bytes',
              'tasks_peak': None, 'cpu_cores': 'cpu', 'disk_delta': 'disk_bytes'}


def _numbers(resources):
    def number(key):
        value = resources.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
    found = {'memory_nonreclaimable_peak': number('memory_nonreclaimable_peak_bytes'),
             'memory_peak': number('memory_peak_bytes'), 'tasks_peak': number('tasks_peak'),
             'disk_delta': number('disk_delta_bytes')}
    cpu, seconds = number('cpu_usage_usec'), number('execution_seconds')
    if cpu is not None and seconds:
        found['cpu_cores'] = cpu/1e6/seconds
    return {key: value for key, value in found.items() if value is not None}


def record(db, handler, attempt, result, outcome, need, now, window=WINDOW, max_age=MAX_AGE_SECONDS, admit=None):
    """Insert this attempt's figures and trim, inside the caller's transaction.
    `outcome` is `succeeded` or `resource_limit`; anything else is ignored. `need` is the
    job's ENFORCED vector (spec.need: the cgroup caps); `admit` is the placement charge
    (what `attempts.need` holds). They differ, and the audit compares against both."""
    if outcome not in ('succeeded', 'resource_limit') or not isinstance(result, dict):
        return 0
    resources = result.get('resources')
    if not isinstance(resources, dict):
        return 0
    def vector(value):
        try:
            value = json.loads(value) if isinstance(value, str) else (value or {})
        except ValueError:
            value = {}
        return value if isinstance(value, dict) else {}
    need, admit = vector(need), vector(admit)
    inserted = 0
    for dimension, value in _numbers(resources).items():
        key = DIMENSIONS[dimension]
        def figure(vec):
            found = vec.get(key) if key else None
            return found if isinstance(found, (int, float)) and not isinstance(found, bool) else None
        db.execute('INSERT OR IGNORE INTO resource_history(handler,dimension,attempt,value,declared,admitted,outcome,at) '
                   'VALUES(?,?,?,?,?,?,?,?)', (handler, dimension, attempt, value, figure(need), figure(admit), outcome, now))
        db.execute('DELETE FROM resource_history WHERE handler=? AND dimension=? AND (at<? OR id NOT IN '
                   '(SELECT id FROM resource_history WHERE handler=? AND dimension=? ORDER BY at DESC,id DESC LIMIT ?))',
                   (handler, dimension, now-max_age, handler, dimension, window))
        inserted += 1
    return inserted


def backfill(db, now, window=WINDOW):
    """Fill an EMPTY table from the last `window` ended attempts per handler, in one
    bounded pass. Returns how many attempts were read (stated, not silent)."""
    if db.execute('SELECT 1 FROM resource_history LIMIT 1').fetchone():
        return 0
    rows = db.execute(
        "SELECT * FROM (SELECT a.id,a.need,a.result,a.created,json_extract(j.spec,'$.need') spec_need,"
        " json_extract(j.spec,'$.handler') handler,"
        " row_number() OVER (PARTITION BY json_extract(j.spec,'$.handler') ORDER BY a.created DESC) rn "
        " FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.state='ended' AND a.result IS NOT NULL "
        " AND json_extract(a.result,'$.result.resources') IS NOT NULL) WHERE rn<=?", (window,)).fetchall()
    for row in rows:
        try:
            completion = json.loads(row['result'])
        except ValueError:
            continue
        outcome = completion.get('outcome')
        if outcome == 'infrastructure':
            from .store import _limit_breach
            outcome = 'resource_limit' if _limit_breach(completion.get('result'), row['spec_need']) else outcome
        record(db, row['handler'], row['id'], completion.get('result'), outcome, row['spec_need'], row['created'],
               window=window, max_age=10**12, admit=row['need'])
    return len(rows)


def _percentile(sorted_values, fraction):
    index = max(0, math.ceil(fraction*len(sorted_values))-1)
    return sorted_values[index]


def summary(db, now, max_age=MAX_AGE_SECONDS, handler=None, same_declaration=True):
    """{handler: {dimension: {n, p50, p95, max, declared, outcomes}}} from ONE statement.
    `declared` is the declaration of the newest row; with `same_declaration` only rows
    that ran under it are counted (a declaration changed by hand starts a fresh comparison)."""
    series = {}
    for row in db.execute('SELECT handler,dimension,value,declared,admitted,outcome FROM resource_history WHERE at>=? '
                          'AND (? IS NULL OR handler=?) ORDER BY handler,dimension,at DESC,id DESC',
                          (now-max_age, handler, handler)):
        series.setdefault((row['handler'], row['dimension']), []).append(row)
    result = {}
    for (handler, dimension), rows in series.items():
        # Only the rows run under the newest declaration are comparable to it.
        declared = rows[0]['declared']
        same = [r for r in rows if r['declared'] == declared] if same_declaration else rows
        values = sorted(r['value'] for r in same)
        result.setdefault(handler, {})[dimension] = dict(
            n=len(values), p50=_percentile(values, .5), p95=_percentile(values, .95), max=values[-1],
            declared=declared, admitted=rows[0]['admitted'], limited=sum(1 for r in same if r['outcome'] == 'resource_limit'))
    return result


def audit(history, min_samples=MIN_SAMPLES):
    """Flags for memory declarations that disagree with what ran. Fewer than `min_samples`
    never flags. Judged on the
    non-reclaimable peak when it exists, else the cgroup peak (design R4). CPU and disk are
    recorded but not audited: CPU is capped by quota (mean cores cannot exceed the cap) and
    the disk figure is a filesystem-wide upper bound, so a flag on either would mislead."""
    flags = []
    for handler, dimensions in sorted(history.items()):
        stats = dimensions.get('memory_nonreclaimable_peak') or dimensions.get('memory_peak')
        if not stats or stats['n'] < min_samples or stats['declared'] is None:
            continue
        observed = max(stats['max'], stats['p95'])
        figures = dict(handler=handler, dimension='memory', declared=stats['declared'], n=stats['n'],
                       p50=stats['p50'], p95=stats['p95'], max=stats['max'])
        if stats['declared'] < observed:
            flags.append(dict(figures, kind='declared_below_observed', suggested=math.ceil(observed*SUGGEST_MARGIN)))
        elif stats['max'] > 0 and stats['declared'] > 4*stats['max']:
            flags.append(dict(figures, kind='declared_far_above_observed', info=True))
        if stats['admitted'] is not None and stats['admitted'] < stats['p50']:
            flags.append(dict(figures, kind='admit_below_typical', admit=stats['admitted']))
    return flags
