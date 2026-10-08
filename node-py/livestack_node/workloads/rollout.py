"""Rollout desired state and the pure planner (observe mode only in this release).

openspec/changes/declarative-worker-rollout, D1 + D5. `rollout-spec.v1` says which deployment unit
(unit.py) each capability set of workers should run; `plan` compares that with the roster and reports
drift and what a reconciler WOULD do next. `plan` is pure (no I/O, no clock besides `now`), so it is
table-tested against the incidents.

This module has no executor. Mode `enforce` is refused by `validate_spec` until an operator-approved
release adds one; `observe` computes and reports, touching no claim and no worker.
"""
from __future__ import annotations

import json
import re

from .model import WorkloadError, name
from . import unit as units
from .smoke import MINIMUM, PROBES

VERSION = 'rollout-spec.v1'
MODES = ('off', 'observe')          # 'enforce' is deliberately absent: operator-only, not yet implemented
ALL_MODES = ('off', 'observe', 'enforce')
MAX_SPEC_BYTES = 64 * 1024
MAX_SETS = 32
MAX_UNITS = 64
ROLLOUT_OWNER = 'rollout'
ATTEMPT_CAP = 3
_UNIT_ID = re.compile(r'^unit-[0-9a-f]{8}$')
_SET_KEYS = {'selector', 'unit', 'min_claiming', 'canary', 'smoke', 'window', 'max_unavailable',
             'soak_seconds', 'failure_budget'}
_SPEC_KEYS = {'version', 'mode', 'sets', 'workers'}
_WORKER_KEYS = {'unit', 'hold'}


def _fail(reason):
    raise WorkloadError(reason, 400)


def _int(value, field, low, high=None):
    if type(value) is not int or value < low or (high is not None and value > high):
        _fail(f'rollout_spec_invalid_{field}')
    return value


def validate_spec(spec, *, allow_enforce=False):
    """Closed keys, bounded. Returns the normalized spec or raises WorkloadError(400, name)."""
    if not isinstance(spec, dict) or set(spec) - _SPEC_KEYS or {'version', 'mode', 'sets'} - set(spec):
        _fail('rollout_spec_unknown_or_missing_fields')
    if spec['version'] != VERSION:
        _fail('rollout_spec_version_unsupported')
    if spec['mode'] == 'enforce' and not allow_enforce:
        _fail('rollout_enforce_not_available: enforce mode is operator-only and not implemented in this release')
    if spec['mode'] not in ALL_MODES:
        _fail('rollout_spec_invalid_mode')
    if len(json.dumps(spec, separators=(',', ':'))) > MAX_SPEC_BYTES:
        _fail('rollout_spec_too_large')
    sets = spec['sets']
    if not isinstance(sets, dict) or len(sets) > MAX_SETS:
        _fail('rollout_spec_invalid_sets')
    for set_name, body in sets.items():
        name(set_name, 'set')
        if not isinstance(body, dict) or set(body) - _SET_KEYS or {'selector', 'min_claiming'} - set(body):
            _fail(f'rollout_spec_set_fields:{set_name}')
        selector = body['selector']
        if (not isinstance(selector, dict) or len(selector) != 1 or
                next(iter(selector)) not in ('ids', 'id_prefix', 'labels')):
            _fail(f'rollout_spec_selector:{set_name}')
        kind, value = next(iter(selector.items()))
        if kind == 'ids':
            if not isinstance(value, list) or not 1 <= len(value) <= 128:
                _fail(f'rollout_spec_selector:{set_name}')
            [name(v, 'worker') for v in value]
        elif kind == 'id_prefix':
            name(value, 'worker prefix')
        elif not isinstance(value, dict) or not 1 <= len(value) <= 8 or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
            _fail(f'rollout_spec_selector:{set_name}')
        if body.get('unit') is not None and (not isinstance(body['unit'], str) or not _UNIT_ID.fullmatch(body['unit'])):
            _fail(f'rollout_spec_unit:{set_name}')
        minimum = body['min_claiming']
        if isinstance(minimum, bool) or not (
                (type(minimum) is int and minimum >= 1) or (type(minimum) is float and 0 < minimum <= 1)):
            _fail(f'rollout_spec_min_claiming_below_one:{set_name}')
        canary = body.get('canary', 'auto')
        if canary != 'auto':
            name(canary, 'canary')
        smoke = body.get('smoke', list(MINIMUM))
        if (not isinstance(smoke, list) or len(set(smoke)) != len(smoke) or any(p not in PROBES for p in smoke)
                or not set(MINIMUM) <= set(smoke)):
            _fail(f'rollout_spec_smoke_below_minimum:{set_name}')
        window = body.get('window')
        if window is not None and (not isinstance(window, dict) or set(window) != {'start_hour', 'end_hour'}
                                   or any(type(window[k]) is not int or not 0 <= window[k] <= 24 for k in window)):
            _fail(f'rollout_spec_window:{set_name}')
        _int(body.get('max_unavailable', 1), 'max_unavailable', 1, 32)
        _int(body.get('soak_seconds', 600), 'soak_seconds', 0, 86400)
        _int(body.get('failure_budget', 3), 'failure_budget', 1, 100)
    workers = spec.get('workers', {})
    if not isinstance(workers, dict) or len(workers) > 256:
        _fail('rollout_spec_invalid_workers')
    for worker, body in workers.items():
        name(worker, 'worker')
        if not isinstance(body, dict) or set(body) - _WORKER_KEYS or (
                body.get('unit') is not None and not _UNIT_ID.fullmatch(str(body['unit']))) or (
                'hold' in body and type(body['hold']) is not bool):
            _fail(f'rollout_spec_worker:{worker}')
    return spec


# ---- state in the authority database ---------------------------------------------------------------

def _get(db, key):
    row = db.execute('SELECT generation, body, updated_at FROM rollout_state WHERE key=?', (key,)).fetchone()
    return None if row is None else dict(generation=row['generation'], body=json.loads(row['body']),
                                         updated_at=row['updated_at'])


def _put(db, key, body, now, if_generation=None):
    row = db.execute('SELECT generation FROM rollout_state WHERE key=?', (key,)).fetchone()
    have = 0 if row is None else row['generation']
    if if_generation is not None and (type(if_generation) is not int or if_generation != have):
        raise WorkloadError(f'rollout_generation_conflict: have {have}, caller expected {if_generation}', 409)
    db.execute('INSERT INTO rollout_state(key,generation,body,updated_at) VALUES(?,?,?,?) '
               'ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,body=excluded.body,'
               'updated_at=excluded.updated_at',
               (key, have + 1, json.dumps(body, separators=(',', ':'), sort_keys=True), now))
    return have + 1


class RolloutState:
    def __init__(self, store, ledger=None):
        self.store, self.ledger = store, ledger

    def set_spec(self, spec, *, if_generation, actor):
        spec = validate_spec(spec)
        now = self.store.clock()
        with self.store.transaction() as db:
            generation = _put(db, 'spec', spec, now, if_generation)
        if self.ledger is not None:
            self.ledger.append('rollout_spec', actor=actor, generation=generation, mode=spec['mode'],
                               sets=sorted(spec['sets']))
        return dict(generation=generation)

    def put_unit(self, manifest):
        units.validate(manifest)
        uid = units.unit_id(manifest)
        now = self.store.clock()
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM rollout_state WHERE key=?', ('unit:' + uid,)).fetchone() is None:
                count = db.execute("SELECT COUNT(*) FROM rollout_state WHERE key LIKE 'unit:%'").fetchone()[0]
                if count >= MAX_UNITS:  # bounded: the oldest manifest goes first
                    db.execute("DELETE FROM rollout_state WHERE key=(SELECT key FROM rollout_state "
                               "WHERE key LIKE 'unit:%' ORDER BY updated_at LIMIT 1)")
                _put(db, 'unit:' + uid, manifest, now)
        return dict(id=uid)

    def put_report(self, report):
        """The reconciler's last observation: one row, overwritten (bounded by construction)."""
        raw = json.dumps(report, separators=(',', ':'))
        if len(raw) > 256 * 1024:
            raise WorkloadError('rollout_report_too_large', 413)
        with self.store.transaction() as db:
            return dict(generation=_put(db, 'report', report, self.store.clock()))

    def read(self, db=None):
        def load(db):
            spec, report = _get(db, 'spec'), _get(db, 'report')
            manifests = {r['key'][5:]: json.loads(r['body']) for r in
                         db.execute("SELECT key, body FROM rollout_state WHERE key LIKE 'unit:%'")}
            return spec, report, manifests
        if db is not None:
            return load(db)
        with self.store.transaction() as conn:
            return load(conn)

    def status(self):
        spec, report, manifests = self.read()
        return dict(
            mode=(spec['body']['mode'] if spec else 'off'),
            spec_generation=spec['generation'] if spec else 0,
            spec=spec['body'] if spec else None, units=sorted(manifests), manifests=manifests,
            reconciler_report=(None if report is None else dict(
                generation=report['generation'], age_s=round(self.store.clock() - report['updated_at'], 1),
                body=report['body'])))


# ---- selection, drift, planner -----------------------------------------------------------------------

def members(selector, entries):
    kind, value = next(iter(selector.items()))
    if kind == 'ids':
        return [e for e in entries if e['id'] in set(value)]
    if kind == 'id_prefix':
        return [e for e in entries if e['id'].startswith(value)]
    return [e for e in entries if all((e.get('labels') or {}).get(k) == v for k, v in value.items())]


def _releases(entry):
    """{handler: release digest(s)} a roster entry runs (digests are 16-char prefixes in the roster)."""
    out = {}
    for item in entry.get('handler_releases') or ():
        out.setdefault(item['handler'], set()).add(item['release'])
    return out


def set_drift(peers):
    """Differences between same-set peers: what makes 'same role' workers not the same.

    handlers_skew:<h>           some peers serve handler h, others do not (2026-10-08: `e2e.task` only on -2)
    handler_release_skew:<h>    peers serve h from different releases (image handler on e2e-1 vs -3/-4/-5)
    label_skew:<key>            peers disagree on a label"""
    found = []
    live = [p for p in peers if p.get('connected')]
    if len(live) < 2:
        return found
    handler_sets = {p['id']: set(p.get('handlers') or ()) for p in live}
    for handler in sorted(set().union(*handler_sets.values())):
        have = sorted(w for w, hs in handler_sets.items() if handler in hs)
        lack = sorted(set(handler_sets) - set(have))
        if lack:
            found.append(dict(kind=f'handlers_skew:{handler}', serving=have, lacking=lack))
    releases = {p['id']: _releases(p) for p in live}
    for handler in sorted({h for r in releases.values() for h in r}):
        groups = {}
        for worker, r in releases.items():
            if handler in r:
                groups.setdefault(tuple(sorted(r[handler])), []).append(worker)
        if len(groups) > 1:
            found.append(dict(kind=f'handler_release_skew:{handler}',
                              groups=[dict(releases=list(k), workers=sorted(v)) for k, v in sorted(groups.items())]))
    label_keys = sorted({k for p in live for k in (p.get('labels') or {})})
    for key in label_keys:
        groups = {}
        for p in live:
            groups.setdefault((p.get('labels') or {}).get(key), []).append(p['id'])
        if len(groups) > 1:
            found.append(dict(kind=f'label_skew:{key}', groups=[
                dict(value=k, workers=sorted(v)) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))]))
    return found


def _min_claiming(rule, size):
    if type(rule) is float:
        return max(1, -(-int(rule * 1000) * size // 1000))   # ceil without float error
    return rule


def _claiming(entry):
    """Connected and not drained: would take work if it arrived."""
    return bool(entry.get('connected')) and entry.get('claim_enabled') is not False


def _in_window(window, hour):
    if not window:
        return True
    start, end = window['start_hour'], window['end_hour']
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def plan(spec, entries, unit_manifests, *, hour_utc=0, memory=None):
    """Compare desired state with the roster. Pure.

    spec: a validated spec (or None). entries: roster worker entries (roster.build()['workers'] shape).
    unit_manifests: {unit id: manifest}. memory: {'rejected': {(set, unit): probe}, 'attempts': {(worker,
    unit): n}, 'failures': {set: n}} from earlier runs (empty in observe). Returns
    {'mode', 'sets': {name: {...}}, 'workers': {id: {...}}}; the actions are intentions, never effects."""
    memory = memory or {}
    out = dict(mode=spec['mode'] if spec else 'off', sets={}, workers={})
    if not spec:
        return out
    pins = spec.get('workers', {})
    claimed_by = set()
    for set_name, body in sorted(spec['sets'].items()):
        peers = members(body['selector'], entries)
        for peer in peers:
            claimed_by.add(peer['id'])
        unit_id = body.get('unit')
        manifest = unit_manifests.get(unit_id) if unit_id else None
        result = dict(unit=unit_id, workers=[p['id'] for p in peers], drift=set_drift(peers), actions=[],
                      waiting=None, states={})
        if unit_id and manifest is None:
            result['waiting'] = f'unit_unknown:{unit_id}'
        for peer in peers:
            pin = pins.get(peer['id'], {})
            want_id = pin.get('unit') or unit_id
            want = units.declared_parts(unit_manifests[want_id]) if want_id in unit_manifests else None
            state, differing = units.unit_state((peer.get('unit') if isinstance(peer.get('unit'), dict) else None),
                                                want)
            skipped = ('hold' if pin.get('hold') else 'stale' if not peer.get('connected')
                       else 'needs_operator' if memory.get('attempts', {}).get((peer['id'], want_id), 0) >= ATTEMPT_CAP
                       else None)
            result['states'][peer['id']] = dict(unit_state=state, differing=differing, desired=want_id, skipped=skipped)
            out['workers'][peer['id']] = dict(set=set_name, unit_state=state, desired=want_id, skipped=skipped)
        if manifest is None or not unit_id or spec['mode'] == 'off':
            out['sets'][set_name] = result
            continue
        rejected = memory.get('rejected', {}).get((set_name, unit_id))
        if rejected:
            result['waiting'] = f'rejected:{rejected}'
            out['sets'][set_name] = result
            continue
        if memory.get('failures', {}).get(set_name, 0) >= 2:
            result['waiting'] = 'paused: failures'
            out['sets'][set_name] = result
            continue
        todo = [p for p in peers if result['states'][p['id']]['unit_state'] not in ('current', 'unknown', 'undeclared')
                and not result['states'][p['id']]['skipped']]
        if not todo:
            out['sets'][set_name] = result
            continue
        busy = [p for p in peers if (p.get('drain') or {}).get('owner') == ROLLOUT_OWNER
                and (p.get('drain') or {}).get('draining')]
        floor = _min_claiming(body['min_claiming'], len(peers))
        if len(busy) >= body.get('max_unavailable', 1):
            result['waiting'] = 'max_unavailable'
        elif not _in_window(body.get('window'), hour_utc):
            result['waiting'] = 'outside_window'
        else:
            canary = _canary(body, peers, todo)
            if canary.get('refusal'):
                result['waiting'] = canary['refusal']
            else:
                target = canary['worker']
                if sum(1 for p in peers if _claiming(p) and p['id'] != target) < floor:
                    result['waiting'] = 'min_claiming'
                else:
                    result['actions'] = [dict(step=step, worker=target, unit=unit_id, observed_only=True)
                                         for step in ('stage', 'drain', 'wait_idle', 'activate', 'smoke', 'enable')]
        out['sets'][set_name] = result
    for entry in entries:
        out['workers'].setdefault(entry['id'], dict(set=None, unit_state='undeclared', desired=None, skipped=None))
    return out


def _canary(body, peers, todo):
    declared = body.get('canary', 'auto')
    eligible = [p for p in todo if p.get('connected')]
    if declared != 'auto':
        chosen = next((p for p in peers if p['id'] == declared), None)
        if chosen is None or not chosen.get('connected'):
            return dict(refusal=f'canary_unavailable:{declared}')
        return dict(worker=declared)
    idle = sorted((p for p in eligible if p.get('state') == 'idle'), key=lambda p: p['id'])
    pool = idle or sorted(eligible, key=lambda p: p['id'])
    if not pool:
        return dict(refusal='no_canary_candidate')
    union = set().union(*(set(p.get('handlers') or ()) for p in peers if p.get('connected')))
    for candidate in pool:
        if set(candidate.get('handlers') or ()) >= union:
            return dict(worker=candidate['id'])
    lacking = sorted(union - set(pool[0].get('handlers') or ()))
    return dict(refusal='canary_not_representative:' + ','.join(lacking))
