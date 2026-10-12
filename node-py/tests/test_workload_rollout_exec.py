"""The rollout executor (rollout_exec.py) against a REAL authority for claims, with the ssh transport replaced by a
model of the host helper (host_release.py is tested for real against systemd in test_workload_host_release.py).

The sequence, the safety rules and each way it can fail are what is under test: never interrupt a job, keep the
minimum claiming, one worker at a time, drain with an expiry and never enable somebody else's drain, roll back and
reject on a failed canary, stop after the failure budget, fail closed, and honour the kill switch."""
import json
from threading import Thread

import pytest

from livestack_node.workloads import claims as claims_module
from livestack_node.workloads import reconciler, rollout, rollout_exec, unit
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore

ADMIN, ROLL = 'm' * 32, 'r' * 32
IDS = [f'zz-joe-e2e-{i}' for i in range(1, 4)]
TOKENS = {w: chr(100 + i) * 32 for i, w in enumerate(IDS)}
NEW, OLD = 'livestack-aaaaaaaa', 'livestack-bbbbbbbb'
H_NEW, H_OLD = 'a' * 64, 'b' * 64
MANIFEST = dict(version='deployment-unit.v1', release=dict(name=NEW, content_hash=H_NEW), handlers=['5' * 64],
                verifier=None, capture=dict(size_bytes=1, cap_bytes=2), min_authority='main', built_from=['a' * 12])
UNIT = unit.unit_id(MANIFEST)
SPEC = dict(version='rollout-spec.v1', mode='observe', workers={}, sets={
    'e2e': dict(selector=dict(id_prefix='zz-joe-e2e-'), unit=UNIT, min_claiming=1)})


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Hosts:
    """What the host helper would answer. `fail[op]` is the reply to give instead."""
    def __init__(self, workers):
        self.hosts = {'zz-joe': ['ubuntu@host']}
        self.state = {w: dict(release=OLD, content_hash=H_OLD, busy=False) for w in workers}
        self.calls, self.fail, self.present, self.pushed = [], {}, set(), []

    def helper(self, host, op, args, timeout=300):
        self.calls.append((op, args.get('worker')))
        if op in self.fail:
            return self.fail[op]
        w = self.state.get(args.get('worker'))
        if op == 'status':
            here = dict(ok=True, release=w['release'], content_hash=w['content_hash'], active='active', busy=w['busy'])
            if args.get('release'):
                here.update(target_present=args['release'] in self.present, target_content_hash=H_NEW if args['release'] in self.present else None)
            return here
        if op == 'install':
            self.present.add(args['release'])
            return dict(ok=True)
        if op == 'switch':
            w['previous'] = dict(w)
            w.update(release=NEW, content_hash=H_NEW)
            return dict(ok=True)
        if op == 'smoke':
            return dict(ok=True, outcome=dict(ok=True))
        if op == 'rollback':
            w.update(w.pop('previous'))
            return dict(ok=True)
        raise AssertionError(op)

    def push_release(self, host, source, name, releases_dir):
        self.pushed.append(name)


@pytest.fixture
def world(tmp_path):
    plist = [dict(id='ops', token=ADMIN, role='admin', handlers=['test.v1']), dict(id='reconciler', token=ROLL, role='rollout')]
    plist += [dict(id=w, token=t, role='worker', worker=w, host=w) for w, t in TOKENS.items()]
    store = WorkloadStore(tmp_path / 'jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal(**p) for p in plist])
    server.claims.sync_file(server.principals)
    Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    ops, roll = WorkloadClient(url, ADMIN, timeout=10), WorkloadClient(url, ROLL, timeout=10)
    ops.request('rollout/units', dict(manifest=MANIFEST))
    ops.request('rollout/spec', dict(spec=SPEC))
    w = type('World', (), {})()
    w.ops, w.roll, w.tmp, w.clock, w.hosts = ops, roll, tmp_path, Clock(), Hosts(IDS)
    w.notified = tmp_path / 'notified'
    w.cfg = dict(enabled=True, kill_file=str(tmp_path / 'KILL'), soak_seconds=600, failure_budget=2, attempt_cap=3,
                 idle_timeout_seconds=1800, verify_timeout_seconds=180, release_source=str(tmp_path),
                 groups=[dict(name='canary', set='e2e', unit=UNIT, workers=['zz-joe-e2e-3'])],
                 notify_cmd=f'echo >> {w.notified}; true')
    w.running = {}
    w.draining_other = {}
    (tmp_path / NEW).mkdir()
    w.executor = rollout_exec.Executor(roll, w.hosts, lambda: w.cfg, rollout_exec.StateFile(tmp_path / 'state.json'),
                                       claims_module.ActionLedger(tmp_path / 'ledger.jsonl'), clock=w.clock, sleep=lambda s: None)

    def roster_drain(claim):   # the roster shows remaining seconds, the claims listing shows the expiry time
        import time
        if claim and claim.get('expires_at'):
            claim = dict(claim, expires_in_s=claim['expires_at'] - time.time())
        return claim

    def tick():
        status = roll.request('rollout')
        claims = {c['worker']: c for c in ops.request('claims')['claims']}
        entries = [dict(id=i, connected=True, state='running' if w.running.get(i) else 'idle', host='zz-joe', running=w.running.get(i, []),
                        claim_enabled=not claims.get(i, {}).get('draining'), labels={}, handlers=['x'], activation_failures=[],
                        last_seen_age_s=1.0, drain=roster_drain(claims.get(i))) for i in IDS]
        return w.executor.tick(status, entries)
    w.tick = tick
    w.draining = lambda i: {c['worker']: c for c in ops.request('claims')['claims']}.get(i, {}).get('draining', False)
    w.kinds = lambda events: [e['kind'] for e in events]
    w.state = lambda: json.loads((tmp_path / 'state.json').read_text())
    w.notes = lambda: len(w.notified.read_text().splitlines()) if w.notified.exists() else 0
    yield w
    server.shutdown(); server.server_close()


def run_to_soak(w):
    events = w.tick()
    assert w.state()['active']['step'] == 'soak', events
    return events


def test_happy_path_drains_switches_smokes_enables_soaks_and_finishes(world):
    w = world
    events = run_to_soak(w)
    assert w.kinds(events) == ['start', 'staged', 'drained', 'switched', 'smoke_passed', 'enabled']
    assert [c for c in w.hosts.calls if c[0] in ('install', 'switch', 'smoke')] == [('install', None), ('switch', 'zz-joe-e2e-3'), ('smoke', 'zz-joe-e2e-3')]
    assert w.hosts.pushed == [NEW]
    assert not w.draining('zz-joe-e2e-3'), 'enabled again after smoke'
    assert w.tick() == [] and w.state()['active']['step'] == 'soak'                     # still soaking: nothing to do
    w.clock.now += 601
    assert w.kinds(w.tick()) == ['done']
    assert w.state()['active'] is None and w.state()['failures'] == {'canary': 0}
    assert w.notes() == 1 and w.tick() == [] , 'finished: the worker is on the declared release, nothing more to do'
    assert not any(w.draining(i) for i in IDS)


def test_only_the_listed_worker_is_touched_and_a_current_worker_is_left_alone(world):
    w = world
    w.hosts.state['zz-joe-e2e-3'].update(release=NEW, content_hash=H_NEW)
    assert w.tick() == [] and not [c for c in w.hosts.calls if c[0] in ('switch', 'install')]
    assert [c for c in w.hosts.calls] == [('status', 'zz-joe-e2e-3')]
    assert w.tick() == [] and len(w.hosts.calls) == 1, 'a verified worker is not re-probed every tick'


def test_a_running_job_is_never_interrupted_and_the_wait_times_out_cleanly(world):
    w = world
    w.running['zz-joe-e2e-3'] = [{'job': 'j1'}]
    w.tick()
    assert w.state()['active']['step'] == 'wait_idle' and w.draining('zz-joe-e2e-3')
    assert not [c for c in w.hosts.calls if c[0] == 'switch']
    w.clock.now += 1801
    assert w.kinds(w.tick()) == ['aborted']
    assert not w.draining('zz-joe-e2e-3') and w.state()['active'] is None
    assert not [c for c in w.hosts.calls if c[0] == 'switch']
    assert w.tick() == [], 'cooldown after an abort'
    w.running.clear(); w.clock.now += 901
    assert 'start' in w.kinds(w.tick())


def test_failed_smoke_rolls_back_rejects_the_release_and_the_second_failure_pauses(world):
    w = world
    w.hosts.fail['smoke'] = dict(ok=False, failure='smoke_failed:release_import:undefined name')
    events = w.tick()
    assert w.kinds(events)[-3:] == ['failed', 'rolled_back', 'rejected']
    assert w.hosts.state['zz-joe-e2e-3']['release'] == OLD, 'the host was put back'
    assert ('rollback', 'zz-joe-e2e-3') in w.hosts.calls
    assert not w.draining('zz-joe-e2e-3'), 'enabled again after the rollback'
    st = w.state()
    assert st['rejected'] == {'zz-joe-e2e-3': [NEW]} and st['failures'] == {'canary': 1} and st['active'] is None
    assert w.notes() == 1
    assert w.tick() == [], 'a rejected release is not retried for that worker'
    # a second, different release fails too: the group pauses
    w.cfg['groups'][0]['unit'] = UNIT
    st['rejected'] = {}; st['failures'] = {'canary': 1}; (w.tmp / 'state.json').write_text(json.dumps(st))
    w.tick()
    assert w.state()['paused'] == {'canary': 'failures:2'}
    assert w.notes() == 2
    w.hosts.fail.clear()
    assert w.tick() == [], 'paused groups do nothing until an operator clears them'


def test_a_failed_rollback_leaves_the_worker_drained_needs_operator_and_pauses_everything(world):
    w = world
    w.hosts.fail['smoke'] = dict(ok=False, failure='smoke_failed:handler_import:x')
    w.hosts.fail['rollback'] = dict(ok=False, failure='not_healthy_after_rollback')
    events = w.tick()
    assert 'rollback_failed' in w.kinds(events)
    claim = {c['worker']: c for c in w.ops.request('claims')['claims']}['zz-joe-e2e-3']
    assert claim['draining'] and claim['needs_operator']
    assert w.state()['paused']['*'].startswith('rollback_failed:zz-joe-e2e-3')
    assert w.tick() == []


def test_a_failure_during_the_soak_re_drains_and_rolls_back(world):
    w = world
    run_to_soak(w)
    w.hosts.state['zz-joe-e2e-3']['previous'] = dict(release=OLD, content_hash=H_OLD, busy=False)
    original = w.hosts.helper
    def unhealthy(host, op, args, timeout=300):
        reply = original(host, op, args, timeout)
        if op == 'status' and not args.get('release'):
            reply = dict(reply, active='failed')
        return reply
    w.hosts.helper = unhealthy
    w.clock.now += 60
    events = w.tick()
    assert 'rolled_back' in w.kinds(events) and w.hosts.state['zz-joe-e2e-3']['release'] == OLD
    assert w.state()['rejected'] == {'zz-joe-e2e-3': [NEW]}


def test_pre_switch_host_refusals_change_nothing_and_are_not_a_release_failure(world):
    w = world
    w.hosts.fail['switch'] = dict(ok=False, failure='worker_busy')
    events = w.tick()
    assert 'aborted' in w.kinds(events) and 'rejected' not in w.kinds(events), 'a job that slipped in after the idle check: the host lock refused'
    assert ('rollback', 'zz-joe-e2e-3') not in w.hosts.calls
    assert not w.draining('zz-joe-e2e-3') and w.state()['failures'] == {} and w.state()['rejected'] == {}


def test_somebody_elses_drain_is_never_released_by_the_executor(world):
    w = world
    w.ops.request('claims/zz-joe-e2e-3/drain', dict(owner='human', reason='mine', ttl_seconds=3600))
    events = w.tick()
    assert w.kinds(events) == ['start', 'staged', 'aborted'] and 'drain_refused' in events[-1]['reason']
    assert w.draining('zz-joe-e2e-3'), "the human's drain is still in force"
    assert not [c for c in w.hosts.calls if c[0] == 'switch']


def test_the_minimum_claiming_floor_blocks_a_start(world):
    w = world
    for other in IDS[:2]:
        w.ops.request(f'claims/{other}/drain', dict(owner='human', reason='x', ttl_seconds=3600))
    assert w.tick() == [] and w.hosts.calls == []
    assert w.state().get('active') is None


def test_kill_switch_unwinds_an_early_step_and_leaves_a_soaking_worker_alone(world):
    w = world
    w.running['zz-joe-e2e-3'] = [{'job': 'j1'}]
    w.tick()
    assert w.draining('zz-joe-e2e-3')
    w.cfg['enabled'] = False
    assert 'switched_off' in w.kinds(w.tick())
    assert not w.draining('zz-joe-e2e-3') and w.state()['active'] is None
    w.cfg['enabled'] = True; w.running.clear(); w.clock.now += 901
    run_to_soak(w)
    (w.tmp / 'KILL').write_text('')
    assert w.kinds(w.tick()) == ['switched_off'] and w.state()['active'] is None
    assert w.hosts.state['zz-joe-e2e-3']['release'] == NEW, 'a smoked, enabled worker is not churned by the kill switch'
    assert w.tick() == []


def test_spec_mode_off_stops_new_starts(world):
    w = world
    w.ops.request('rollout/spec', dict(spec=dict(SPEC, mode='off')))
    assert w.tick() == [] and w.hosts.calls == []


def test_an_unreadable_state_file_fails_closed_and_the_reconciler_reports_it(world):
    w = world
    (w.tmp / 'state.json').write_text('{not json')
    with pytest.raises(RuntimeError):
        w.tick()
    events, summary = reconciler.run_executor(w.executor, w.roll.request('rollout'), [])
    assert events == [] and summary['enabled'] is False and 'unreadable' in summary['error']
    assert w.hosts.calls == []


def test_missing_enforce_block_means_no_executor(world, tmp_path):
    path = tmp_path / 'rec.json'
    path.write_text(json.dumps(dict(authority='x', token='y', ledger='z')))
    assert reconciler.make_executor(None, None, path) is None
    assert reconciler.enforce_config(path) == {} and reconciler.enforce_config(tmp_path / 'absent.json') == {}
    assert reconciler.run_executor(None, {}, []) == ([], None)


class LocalShell(rollout_exec.SshTransport):
    """Same push command line, run by a local shell instead of ssh (an ssh server runs it through `sh -c` too)."""
    def _ssh(self, host):
        return ['sh', '-c']


def test_push_release_streams_the_directory_and_keeps_a_tilde_path_expandable(tmp_path, monkeypatch):
    source = tmp_path / 'src' / NEW / 'node-py'
    source.mkdir(parents=True)
    (source / 'a.py').write_text('A = 1\n')
    (source / '__pycache__').mkdir()
    (source / '__pycache__' / 'a.pyc').write_text('x')
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    transport = LocalShell({'h': []}, helper_source='')
    transport.push_release('h', tmp_path / 'src', NEW, '~/.local/share/releases')
    pushed = home / '.local/share/releases' / f'.stage-{NEW}' / NEW / 'node-py'
    assert (pushed / 'a.py').read_text() == 'A = 1\n' and not (pushed / '__pycache__').exists()
    transport.push_release('h', tmp_path / 'src', NEW, str(tmp_path / 'abs releases'))
    assert (tmp_path / 'abs releases' / f'.stage-{NEW}' / NEW / 'node-py' / 'a.py').exists()
    with pytest.raises(rollout_exec.HostFailure):
        transport.push_release('h', tmp_path / 'src' / 'missing', NEW, str(tmp_path / 'x'))


def test_a_drain_is_renewed_while_a_long_job_runs_so_it_cannot_lapse(world):
    w = world
    w.running['zz-joe-e2e-3'] = [{'job': 'j1'}]
    w.tick()
    assert 'drain_renewed' not in w.kinds(w.tick()), 'a fresh drain is left alone'
    w.ops.request('claims/zz-joe-e2e-3/drain', dict(owner='rollout', reason='simulate an old drain', ttl_seconds=600))
    import time
    left = lambda: {c['worker']: c for c in w.ops.request('claims')['claims']}['zz-joe-e2e-3']['expires_at'] - time.time()
    assert left() < 700
    assert 'drain_renewed' in w.kinds(w.tick())
    assert left() > 3000, 'back to the full ttl'


def test_signer_workers_wait_for_an_operator_approval_file_and_ask_once(world):
    w = world
    w.cfg['groups'][0]['requires_approval'] = True
    w.cfg['approve_dir'] = str(w.tmp / 'approvals')
    assert w.kinds(w.tick()) == ['awaiting_approval']
    assert not [c for c in w.hosts.calls if c[0] in ('install', 'switch')] and not w.draining('zz-joe-e2e-3')
    assert w.notes() == 1, 'asked once, not every tick'
    w.tick()
    assert w.notes() == 1
    (w.tmp / 'approvals').mkdir()
    (w.tmp / 'approvals' / f'zz-joe-e2e-3@{NEW}').write_text('')
    assert 'start' in w.kinds(w.tick())
