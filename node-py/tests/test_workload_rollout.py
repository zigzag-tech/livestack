"""Rollout spec, pure planner, roster unit_state and the OBSERVE-mode reconciler.

openspec/changes/declarative-worker-rollout, tasks 2.2-2.3, 3.1-3.4. The planner tests use a fixture
shaped like the live fleet on 2026-10-08 (image handler release on e2e-1 vs -3/-4/-5; `e2e.task` only
on e2e-2). The reconciler test runs against a real authority and proves observe changes nothing.
"""
import copy
import hashlib
import json
from io import BytesIO
from threading import Thread

import pytest

from livestack_node.workloads import claims as claims_module
from livestack_node.workloads import reconciler, rollout, unit
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore

NEW_R, OLD_R, NEW_V, OLD_V = '1'*64, '2'*64, '3'*64, '4'*64
H_NEW = '5'*64
MANIFEST = dict(version='deployment-unit.v1', release=dict(name='livestack-aaaaaaaa', content_hash=NEW_R),
                handlers=[H_NEW], verifier=NEW_V, capture=dict(size_bytes=1, cap_bytes=2),
                min_authority='main', built_from=['a'*12])
UNIT = unit.unit_id(MANIFEST)
SPEC = dict(version='rollout-spec.v1', mode='observe', workers={}, sets={
    'e2e': dict(selector=dict(id_prefix='zz-joe-e2e-'), unit=UNIT, min_claiming=2)})


def entry(i, *, handlers=('image', 'build'), image='img-new', reported=None, drain=None, connected=True,
          state='idle', labels=None, prefix='zz-joe-e2e-'):
    return dict(id=f'{prefix}{i}', connected=connected, state=state, handlers=list(handlers),
                labels=labels or {}, claim_enabled=not (drain or {}).get('draining'), drain=drain,
                handler_releases=[dict(handler='image', release=image)], unit=reported)


CURRENT = dict(release=NEW_R, handlers=[H_NEW], verifier=NEW_V)
OLD = dict(release=OLD_R, handlers=[], verifier=OLD_V)


def fleet(over=None):
    """5 e2e slots, all on the old unit."""
    return [entry(i, reported=OLD, **(over or {}).get(i, {})) for i in range(1, 6)]


def plan(spec=SPEC, entries=None, **kw):
    return rollout.plan(spec, entries if entries is not None else fleet(), {UNIT: MANIFEST}, **kw)


# ---- spec validation (3.1) -------------------------------------------------------------------------------

def test_spec_is_closed_and_refuses_what_the_proposal_forbids():
    assert rollout.validate_spec(copy.deepcopy(SPEC))
    cases = {
        'rollout_spec_unknown_or_missing_fields': lambda s: s.update(extra=1),
        'rollout_enforce_not_available': lambda s: s.update(mode='enforce'),
        'rollout_spec_min_claiming_below_one': lambda s: s['sets']['e2e'].update(min_claiming=0),
        'rollout_spec_min_claiming_below_one ': lambda s: s['sets']['e2e'].update(min_claiming=True),
        'rollout_spec_smoke_below_minimum': lambda s: s['sets']['e2e'].update(smoke=['handler_import']),
        'rollout_spec_set_fields': lambda s: s['sets']['e2e'].update(restart_all=True),
        'rollout_spec_selector': lambda s: s['sets']['e2e'].update(selector={'ids': []}),
        'rollout_spec_unit': lambda s: s['sets']['e2e'].update(unit='livestack-1'),
        'rollout_spec_version_unsupported': lambda s: s.update(version='x'),
    }
    for reason, mutate in cases.items():
        bad = copy.deepcopy(SPEC); mutate(bad)
        with pytest.raises(WorkloadError) as error:
            rollout.validate_spec(bad)
        assert reason.strip() in str(error.value), reason


# ---- planner tables (3.2) -----------------------------------------------------------------------------------

def test_off_mode_and_no_spec_plan_nothing():
    assert plan(None)['sets'] == {}
    off = plan(dict(SPEC, mode='off'))
    assert off['sets']['e2e']['actions'] == [] and off['workers']['zz-joe-e2e-1']['unit_state'] == 'behind'


def test_observe_proposes_one_canary_and_never_more_than_one():
    result = plan()['sets']['e2e']
    assert [a['worker'] for a in result['actions']] == ['zz-joe-e2e-1'] * 6
    assert [a['step'] for a in result['actions']] == ['stage', 'drain', 'wait_idle', 'activate', 'smoke', 'enable']
    assert all(a['observed_only'] for a in result['actions'])


def test_min_claiming_blocks_a_step_that_would_leave_too_few():
    drained = {'owner': 'someone', 'draining': True}
    entries = fleet({3: dict(drain=drained), 4: dict(drain=drained), 5: dict(drain=drained)})
    # 5 members, floor 2: draining a canary leaves 1 claiming (e2e-2 only) -> wait.
    assert plan(entries=entries)['sets']['e2e']['waiting'] == 'min_claiming'
    entries = fleet({4: dict(drain=drained), 5: dict(drain=drained)})
    assert plan(entries=entries)['sets']['e2e']['actions']          # 2 left claiming: allowed


def test_max_unavailable_counts_rollout_drains_only():
    rolling = {'owner': 'rollout', 'draining': True}
    result = plan(entries=fleet({2: dict(drain=rolling)}))['sets']['e2e']
    assert result['waiting'] == 'max_unavailable' and result['actions'] == []
    other = {'owner': 'an-agent', 'draining': True}
    assert plan(entries=fleet({2: dict(drain=other)}))['sets']['e2e']['actions']


def test_hold_stale_and_unknown_are_skipped_and_reported_not_retried():
    entries = fleet({1: dict(connected=False)})
    entries[1]['unit'] = None                                   # legacy worker: no facts
    spec = copy.deepcopy(SPEC); spec['workers'] = {'zz-joe-e2e-3': dict(hold=True)}
    result = plan(spec, entries)
    states = result['sets']['e2e']['states']
    assert states['zz-joe-e2e-1']['skipped'] == 'stale' and states['zz-joe-e2e-3']['skipped'] == 'hold'
    assert states['zz-joe-e2e-2']['unit_state'] == 'unknown'
    assert result['sets']['e2e']['actions'][0]['worker'] == 'zz-joe-e2e-4'   # first one actually actionable


def test_canary_is_one_that_serves_every_handler_else_operator_chooses():
    skew = fleet({i: dict(handlers=('image',)) for i in (1, 3, 4, 5)})   # `e2e.task` only on -2
    for e in skew:
        if e['id'] == 'zz-joe-e2e-2':
            e['handlers'] = ['image', 'e2e.task']
    assert plan(entries=skew)['sets']['e2e']['actions'][0]['worker'] == 'zz-joe-e2e-2'
    spec = copy.deepcopy(SPEC); spec['sets']['e2e']['canary'] = 'zz-joe-e2e-1'
    bad = plan(spec, skew)['sets']['e2e']
    assert bad['actions'][0]['worker'] == 'zz-joe-e2e-1'             # operator's explicit choice is honoured
    only_partial = [e for e in skew if e['id'] != 'zz-joe-e2e-2']
    for e in only_partial: e['unit'] = OLD
    other = copy.deepcopy(only_partial)
    other[0]['handlers'] = ['image', 'e2e.task']                      # a server of e2e.task exists but is not a todo
    other[0]['unit'] = CURRENT
    assert plan(entries=other)['sets']['e2e']['waiting'].startswith('canary_not_representative:e2e.task')


def test_rejected_unit_paused_set_and_attempt_cap():
    memory = dict(rejected={('e2e', UNIT): 'handler_import'})
    assert plan(memory=memory)['sets']['e2e']['waiting'] == 'rejected:handler_import'
    assert plan(memory=dict(failures={'e2e': 2}))['sets']['e2e']['waiting'] == 'paused: failures'
    capped = dict(attempts={('zz-joe-e2e-1', UNIT): 3})
    result = plan(memory=capped)['sets']['e2e']
    assert result['states']['zz-joe-e2e-1']['skipped'] == 'needs_operator'
    assert result['actions'][0]['worker'] == 'zz-joe-e2e-2'


def test_window_and_current_fleet():
    spec = copy.deepcopy(SPEC); spec['sets']['e2e']['window'] = dict(start_hour=2, end_hour=6)
    assert plan(spec, hour_utc=12)['sets']['e2e']['waiting'] == 'outside_window'
    assert plan(spec, hour_utc=3)['sets']['e2e']['actions']
    done = [entry(i, reported=CURRENT) for i in range(1, 6)]
    assert plan(entries=done)['sets']['e2e']['actions'] == []


def test_fraction_floor_rounds_up():
    assert rollout._min_claiming(0.4, 5) == 2 and rollout._min_claiming(0.5, 1) == 1 and rollout._min_claiming(3, 5) == 3


def test_drift_report_reproduces_the_2026_10_08_skew():
    entries = [entry(1, image='img-old', labels={'benchday_image_handler_release': 'a'}),
               entry(2, handlers=('image', 'build', 'e2e.task'), labels={'benchday_image_handler_release': 'b'}),
               entry(3), entry(4), entry(5)]
    for e in entries[2:]:
        e['labels'] = {'benchday_image_handler_release': 'b'}
    kinds = {d['kind']: d for d in rollout.set_drift(entries)}
    assert kinds['handlers_skew:e2e.task']['serving'] == ['zz-joe-e2e-2']
    assert sorted(kinds['handlers_skew:e2e.task']['lacking']) == ['zz-joe-e2e-1', 'zz-joe-e2e-3', 'zz-joe-e2e-4', 'zz-joe-e2e-5']
    groups = {tuple(g['workers']) for g in kinds['handler_release_skew:image']['groups']}
    assert groups == {('zz-joe-e2e-1',), ('zz-joe-e2e-2', 'zz-joe-e2e-3', 'zz-joe-e2e-4', 'zz-joe-e2e-5')}
    assert 'label_skew:benchday_image_handler_release' in kinds
    assert rollout.set_drift(entries[:1]) == []


# ---- a real authority: spec CAS, roster unit_state, observe changes nothing (3.1, 2.2, 2.3, 3.4) -----------

A, ADMIN, ROLL = 'a'*32, 'm'*32, 'r'*32
W = {f'zz-joe-e2e-{i}': chr(100 + i) * 32 for i in range(1, 4)}


@pytest.fixture
def authority(tmp_path):
    plist = [dict(id='alice', token=A, role='caller', handlers=['test.v1']),
             dict(id='ops', token=ADMIN, role='admin', handlers=['test.v1']),
             dict(id='reconciler', token=ROLL, role='rollout')]
    plist += [dict(id=w, token=t, role='worker', worker=w, host=w) for w, t in W.items()]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal(**p) for p in plist])
    server.claims.sync_file(server.principals)
    Thread(target=server.serve_forever, daemon=True).start()
    made = type('Authority', (), {})()
    made.url, made.server, made.tmp = f'http://127.0.0.1:{server.server_port}', server, tmp_path
    made.client = lambda token: WorkloadClient(made.url, token, timeout=10)
    yield made
    server.shutdown(); server.server_close()


def report(unit_part=None):
    body = dict(capacity={'cpu': 2}, available={'cpu': 2}, labels={}, handlers=['test.v1'], ready=True)
    if unit_part is not None:
        body['unit'] = unit_part
    return dict(boot='b1', report=body)


def test_spec_writes_are_compare_and_swap_and_enforce_is_refused(authority):
    roll = authority.client(ROLL)
    first = roll.request('rollout/spec', dict(spec=SPEC, if_generation=0))
    assert first['generation'] == 1
    with pytest.raises(WorkloadError) as stale:
        roll.request('rollout/spec', dict(spec=SPEC, if_generation=0))
    assert stale.value.status == 409 and 'rollout_generation_conflict' in str(stale.value)
    with pytest.raises(WorkloadError) as enforce:
        authority.client(ADMIN).request('rollout/spec', dict(spec=dict(SPEC, mode='enforce')))
    assert 'rollout_enforce_not_available' in str(enforce.value)
    assert authority.client(A).request('rollout')['spec_generation'] == 1                 # callers may read
    with pytest.raises(WorkloadError) as denied:
        authority.client(A).request('rollout/spec', dict(spec=SPEC))
    assert denied.value.status == 403


def test_roster_reports_unit_mismatch_for_new_bundle_with_old_verifier(authority):
    authority.client(ADMIN).request('rollout/units', dict(manifest=MANIFEST))
    authority.client(ADMIN).request('rollout/spec', dict(spec=SPEC))
    parts = {'zz-joe-e2e-1': CURRENT, 'zz-joe-e2e-2': dict(CURRENT, verifier=OLD_V), 'zz-joe-e2e-3': None}
    for worker, part in parts.items():
        authority.client(W[worker]).request('worker/report', report(part))
    states = {w['id']: w['unit_state'] for w in authority.client(ADMIN).request('workers')['workers']}
    assert states == {'zz-joe-e2e-1': 'current', 'zz-joe-e2e-2': 'unit_mismatch:verifier',
                      'zz-joe-e2e-3': 'unknown'}


def test_authority_refuses_a_malformed_unit_report(authority):
    with pytest.raises(WorkloadError):
        authority.client(W['zz-joe-e2e-1']).request('worker/report', report(dict(release='nope')))


def test_a_worker_without_unit_report_is_byte_identical(authority):
    authority.client(W['zz-joe-e2e-1']).request('worker/report', report())
    with authority.server.store.transaction() as db:
        stored = json.loads(db.execute("SELECT report FROM workers WHERE id='zz-joe-e2e-1'").fetchone()[0])
    assert 'unit' not in stored


def test_observe_reports_drift_and_changes_nothing(authority):
    ops = authority.client(ADMIN)
    ops.request('rollout/units', dict(manifest=MANIFEST))
    ops.request('rollout/spec', dict(spec=SPEC))
    for worker in W:
        authority.client(W[worker]).request('worker/report', report(OLD))
    before = ops.request('claims')
    ledger = claims_module.ActionLedger(authority.tmp/'reconciler.jsonl')
    roll = authority.client(ROLL)
    first, intent = reconciler.observe(roll, ledger, None)
    assert first['mode'] == 'observe' and first['applied'] == []
    assert first['unit_state'] == {w: 'behind' for w in W}
    assert first['would_do'] and all(a['observed_only'] for a in first['would_do'])
    # The same observation twice ledgers the intention once.
    reconciler.observe(roll, ledger, intent)
    lines = [json.loads(l) for l in (authority.tmp/'reconciler.jsonl').read_text().splitlines()]
    assert [l['kind'] for l in lines] == ['observe']
    # Nothing was drained, enabled or touched, and the authority holds the observation.
    assert ops.request('claims')['claims'] == before['claims']
    status = ops.request('rollout')
    assert status['reconciler_report']['body']['would_do'] == first['would_do']
    assert all(c['draining'] is False for c in ops.request('claims')['claims'])


def test_reconciler_cannot_drain_force_or_clear_a_hold_beyond_its_role(authority):
    roll = authority.client(ROLL)
    assert roll.request('claims/zz-joe-e2e-1/drain', dict(ttl_seconds=60, owner='rollout'))['owner'] == 'rollout'
    with pytest.raises(WorkloadError) as error:
        roll.request('claims/zz-joe-e2e-1/drain', dict(ttl_seconds=60, force=True))
    assert error.value.status == 403
    with pytest.raises(WorkloadError) as error:
        roll.request('rollout/spec', dict(spec=dict(SPEC, mode='enforce')))
    assert 'rollout_enforce_not_available' in str(error.value)
    with pytest.raises(WorkloadError) as error:
        roll.request('jobs')                 # the reconciler is not a caller
    assert error.value.status == 403
