"""The rollout executor: ENFORCE for an explicit list of workers, one at a time, canary first.

openspec/changes/declarative-worker-rollout, tasks 3.4, 4.x, 5.x, 6.1 (first slice). The reconciler
(reconciler.py) runs this each tick when its config carries an `enforce` block. Spec mode in the authority
stays `observe`; enforcement is a LOCAL, operator-owned decision (the config file), so a rogue spec cannot turn
it on and no authority restart is needed to turn it on or off.

What it does for ONE worker at a time (groups are listed in order; a group is a rollout-spec set):

    stage      push the declared release directory to the worker's host, verify its content hash there
    drain      claims API, owner `rollout`, with an expiry (never an unbounded drain)
    wait_idle  until the roster shows no running attempt. It never interrupts a job: the host helper refuses to
               restart a unit that has an attempt in flight, as a second lock
    switch     host helper: one drop-in selects the release, restart, wait healthy on the new path
    smoke      host helper: restart-clean + handler import/integrity + release_import, run on the host
    verify     roster shows the worker connected, fresh, without activation failures
    enable     claims API
    soak       watch the worker for `soak_seconds`: still connected, no activation failures, no restart

Any failure after `switch` rolls the worker back (the host helper undoes its one drop-in and restarts), marks the
release REJECTED for that worker (never retried until the config or release changes), counts a failure for the
group, and stops the group after `failure_budget` failures (`paused`, notified). A rollback that itself fails
leaves the worker drained with `needs_operator` (an expiry never re-enables it) and pauses everything.

Bounds: one active step fleet-wide; `min_claiming` of the spec set is kept (so a two-worker release set always
keeps one eligible); a per-worker attempt cap; every drain has an expiry. State is one small JSON file, written
atomically after each step so a restarted reconciler resumes or safely abandons. Kill switch: `enforce.enabled`
false, or the `kill_file` existing, or spec mode `off`; an active step is then unwound to a safe state.

Authority restarts are not part of this and never will be: they stay with tools/deploy-authority-release.sh.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from .model import WorkloadError
from . import rollout

OWNER = rollout.ROLLOUT_OWNER
PRE_SWITCH_FAILURES = {'bad_release_name', 'release_missing', 'release_content_hash_mismatch', 'worker_busy',
                       'unit_not_found', 'pythonpath_has_no_single_release_entry', 'managed_dropin_not_effective'}
SMOKE_EXTRA = ('release_import',)
MAX_REJECTED = 64


class HostFailure(Exception):
    def __init__(self, failure, detail=None):
        Exception.__init__(self, failure)
        self.failure, self.detail = failure, detail


def shell_path(path):
    """A remote path for a shell command line: `~/x` must keep its tilde expandable, so it becomes "$HOME"/x."""
    path = str(path)
    return '"$HOME"/' + shlex.quote(path[2:]) if path.startswith('~/') else shlex.quote(path)


class SshTransport:
    """Talks to a worker host over ssh: helper operations (host_release.py piped on stdin) and a tar push."""

    def __init__(self, hosts, helper_source=None, connect_timeout=10):
        self.hosts = hosts
        self.source = helper_source if helper_source is not None else (Path(__file__).with_name('host_release.py')).read_text()
        self.connect_timeout = connect_timeout

    def _ssh(self, host):
        return ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={self.connect_timeout}',
                '-o', 'ServerAliveInterval=15'] + list(self.hosts[host])

    def helper(self, host, op, args, timeout=300):
        """Run one helper op. Returns the parsed JSON (ok true or false); raises HostFailure when the host did
        not answer in the helper's format at all."""
        argv = self._ssh(host) + ['python3', '-', op, shlex.quote(json.dumps(args, separators=(',', ':')))]
        try:
            done = subprocess.run(argv, input=self.source.encode(), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HostFailure('helper_timeout', op)
        text = done.stdout.decode('utf-8', 'replace').strip().splitlines()
        try:
            return json.loads(text[-1])
        except (IndexError, ValueError):
            raise HostFailure('helper_unreadable', (done.stderr.decode('utf-8', 'replace') or 'no output')[-200:])

    def push_release(self, host, source_dir, name, releases_dir, timeout=1200):
        stage = shell_path(f'{releases_dir}/.stage-{name}')
        remote = f'rm -rf {stage} && mkdir -p {stage} && tar -xf - -C {stage}'
        tar = subprocess.Popen(['tar', '--exclude=__pycache__', '-C', str(source_dir), '-cf', '-', name], stdout=subprocess.PIPE)
        try:
            done = subprocess.run(self._ssh(host) + [remote], stdin=tar.stdout, capture_output=True, timeout=timeout)
        finally:
            tar.stdout.close()
            tar.wait()
        if tar.returncode != 0 or done.returncode != 0:
            raise HostFailure('push_failed', done.stderr.decode('utf-8', 'replace')[-200:])


class StateFile:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            state = {}
        except ValueError:
            raise RuntimeError(f'executor state {self.path} is unreadable; refusing to guess')   # fail closed
        state.setdefault('rejected', {}); state.setdefault('failures', {}); state.setdefault('attempts', {})
        state.setdefault('paused', {}); state.setdefault('cooldown', {})
        return state

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(state, sort_keys=True, separators=(',', ':')))
        os.replace(tmp, self.path)


class Executor:
    def __init__(self, client, transport, load_config, state, ledger, *, notify=None, clock=time.time, sleep=time.sleep):
        self.client, self.transport, self.load_config = client, transport, load_config
        self.state_file, self.ledger = state, ledger
        self.notify_cmd, self.clock, self.sleep = notify, clock, sleep
        self.events = []
        self.waiting = {}   # {'group/worker': reason} of the last tick; reported, not ledgered every 30 s

    # ---- small helpers -----------------------------------------------------------------------

    def note(self, kind, **fields):
        event = dict(at=round(self.clock(), 1), kind=kind, **fields)
        self.events.append(event)
        self.ledger.append('rollout_exec_' + kind, **fields)

    def tell(self, cfg, message):
        command = cfg.get('notify_cmd')
        if not command:
            return
        try:
            subprocess.run(['sh', '-c', command + ' "$1"', 'notify', message], capture_output=True, timeout=60)
        except Exception as error:  # a broken notifier must not stop an unwind
            self.note('notify_failed', error=f'{type(error).__name__}')

    def drain(self, cfg, worker, reason, needs_operator=False):
        body = dict(owner=OWNER, reason=reason, ttl_seconds=cfg.get('drain_ttl_seconds', 3600))
        if needs_operator:
            body['needs_operator'] = True
        return self.client.request(f'claims/{worker}/drain', body)

    def enable(self, worker, reason):
        return self.client.request(f'claims/{worker}/enable', dict(owner=OWNER, reason=reason))

    @staticmethod
    def entry_of(entries, worker):
        return next((e for e in entries if e['id'] == worker), None)

    def helper(self, active, op, args, timeout=300):
        return self.transport.helper(active['host'], op, dict(args, worker=active['worker']), timeout)

    # ---- the tick ----------------------------------------------------------------------------

    def tick(self, status, entries):
        """One pass. Returns the events it produced. Raises only for an unreadable state file."""
        self.events = []
        self.waiting = {}
        cfg = self.load_config()
        state = self.state_file.load()
        active = state.get('active')
        off = self.switched_off(cfg, status)
        if active:
            try:
                self.advance(cfg, status, entries, state, active, off)
            finally:
                self.state_file.save(state)
        elif not off:
            try:
                self.select(cfg, status, entries, state)
                if state.get('active'):   # start moving in the same tick: stage and drain are quick
                    self.advance(cfg, status, entries, state, state['active'], None)
            finally:
                self.state_file.save(state)
        return self.events

    @staticmethod
    def switched_off(cfg, status):
        if not cfg.get('enabled'):
            return 'config_disabled'
        if cfg.get('kill_file') and os.path.exists(os.path.expanduser(cfg['kill_file'])):
            return 'kill_file'
        if status.get('mode') == 'off':
            return 'spec_mode_off'
        return None

    # ---- choosing the next worker ------------------------------------------------------------

    def select(self, cfg, status, entries, state):
        if state['paused'].get('*'):
            return
        spec = status.get('spec') or {}
        now = self.clock()
        for group in cfg.get('groups', []):
            if state['paused'].get(group['name']):
                continue
            manifest = status.get('manifests', {}).get(group.get('unit'))
            if manifest is None:
                self.waiting[group['name']] = f"unit_unknown:{group.get('unit')}"
                continue
            release = manifest['release']
            set_body = (spec.get('sets') or {}).get(group.get('set'))
            for worker in group['workers']:
                key = f"{worker}|{release['name']}"
                if release['name'] in state['rejected'].get(worker, []):
                    self.waiting[f"{group['name']}/{worker}"] = 'release_rejected_for_this_worker'
                    continue
                if state['attempts'].get(key, 0) >= cfg.get('attempt_cap', 2):
                    self.waiting[f"{group['name']}/{worker}"] = 'needs_operator:attempt_cap'
                    if key not in state.setdefault('capped', []):
                        state['capped'].append(key)
                        del state['capped'][:-MAX_REJECTED]
                        self.note('attempt_cap', worker=worker, release=release['name'])
                        self.tell(cfg, f'rollout needs an operator: {worker} -> {release["name"]} gave up after {state["attempts"][key]} attempts')
                    continue
                if group.get('requires_approval'):
                    # signer/keystore workers (design D9): an operator approves each worker+release by creating a file
                    approval = Path(cfg.get('approve_dir', '~/.config/livestack-workloads/rollout-approvals')).expanduser() / f"{worker}@{release['name']}"
                    if not approval.exists():
                        self.waiting[f"{group['name']}/{worker}"] = f'awaiting_approval:touch {approval}'
                        asked = state.setdefault('asked', [])
                        if f'{worker}@{release["name"]}' not in asked:
                            asked.append(f'{worker}@{release["name"]}')
                            del asked[:-MAX_REJECTED]
                            self.note('awaiting_approval', worker=worker, release=release['name'])
                            self.tell(cfg, f'rollout awaiting approval: {worker} -> {release["name"]}. Approve with: touch {approval}')
                        continue
                seen = state.setdefault('verified', {}).get(worker)
                if seen and seen[0] == release['name'] and now - seen[1] < cfg.get('recheck_seconds', 600):
                    continue
                if state['cooldown'].get(worker, 0) > now:
                    continue
                entry = self.entry_of(entries, worker)
                host = entry and entry.get('host')
                if entry is None or not entry.get('connected') or host not in self.transport.hosts:
                    self.waiting[f"{group['name']}/{worker}"] = 'worker_not_reachable_or_host_unmapped'
                    continue
                if set_body is None or not self.keeps_minimum(set_body, entries, worker):
                    self.waiting[f"{group['name']}/{worker}"] = 'min_claiming'
                    continue
                if not rollout._in_window(set_body.get('window'), time.gmtime(now).tm_hour):
                    self.waiting[f"{group['name']}/{worker}"] = 'outside_window'
                    continue
                try:
                    here = self.transport.helper(host, 'status', dict(worker=worker, release=release['name']))
                except HostFailure as error:
                    self.waiting[f"{group['name']}/{worker}"] = f'host_status_failed:{error.failure}'
                    continue
                if not here.get('ok'):
                    self.waiting[f"{group['name']}/{worker}"] = f"host_status_failed:{here.get('failure')}"
                    continue
                if here.get('content_hash') == release['content_hash']:
                    state['verified'][worker] = [release['name'], now]   # already on the declared release
                    continue
                state['attempts'][key] = state['attempts'].get(key, 0) + 1
                state['active'] = dict(worker=worker, group=group['name'], set=group.get('set'), host=host, release=release['name'],
                                       content_hash=release['content_hash'], step='stage', started=now, step_started=now,
                                       previous_release=here.get('release'), switched=False,
                                       probes=list(dict.fromkeys(list(set_body.get('smoke', rollout.MINIMUM)) + list(SMOKE_EXTRA))),
                                       soak_seconds=set_body.get('soak_seconds', cfg.get('soak_seconds', 600)))
                self.note('start', worker=worker, group=group['name'], release=release['name'], previous=here.get('release'))
                return   # one active step fleet-wide

    @staticmethod
    def keeps_minimum(set_body, entries, worker):
        peers = rollout.members(set_body['selector'], entries)
        floor = rollout._min_claiming(set_body['min_claiming'], len(peers))
        return sum(1 for p in peers if rollout._claiming(p) and p['id'] != worker) >= floor

    # ---- advancing the active step -----------------------------------------------------------

    def advance(self, cfg, status, entries, state, active, off):
        worker = active['worker']
        if off and active['step'] == 'soak':   # already enabled and smoke-tested: stop watching, change nothing
            state['active'] = None
            self.note('switched_off', worker=worker, reason=off, step='soak')
            return
        if off and active['step'] not in ('rollback', 'unwind'):
            self.note('switched_off', worker=worker, reason=off, step=active['step'])
            self.tell(cfg, f'rollout stopped by {off}: unwinding {worker} (step {active["step"]})')
            active['step'], active['unwind_reason'] = 'unwind', f'switched_off:{off}'
        for _ in range(12):   # a tick can run several quick steps in a row; waiting steps return
            step = active['step']
            if state.get('active') is None:
                return
            try:
                if getattr(self, 'step_' + step)(cfg, status, entries, state, active) != 'continue':
                    return
            except HostFailure as error:
                self.fail(cfg, state, active, f'host:{error.failure}', error.detail)
                return
            except WorkloadError as error:
                self.fail(cfg, state, active, f'authority:{error}', None)
                return
            self.state_file.save(state)

    def goto(self, active, step):
        active['step'], active['step_started'] = step, self.clock()
        return 'continue'

    def step_stage(self, cfg, status, entries, state, active):
        releases = cfg.get('releases_dir')
        args = dict(release=active['release'], releases_dir=releases) if releases else dict(release=active['release'])
        here = self.helper(active, 'status', args)
        if here.get('ok') and here.get('target_present') and here.get('target_content_hash') == active['content_hash']:
            return self.goto(active, 'drain')
        source = Path(cfg.get('release_source', '~/.local/share/livestack-workload-releases')).expanduser()
        if not (source / active['release']).is_dir():
            return self.fail(cfg, state, active, 'stage:release_not_built_on_the_reconciler_host', active['release'])
        self.transport.push_release(active['host'], source, active['release'], releases or '~/.local/share/livestack-workload-releases')
        done = self.transport.helper(active['host'], 'install', dict(args, content_hash=active['content_hash']))
        if not done.get('ok'):
            return self.fail(cfg, state, active, f"stage:{done.get('failure')}", None)
        self.note('staged', worker=active['worker'], release=active['release'])
        return self.goto(active, 'drain')

    def step_drain(self, cfg, status, entries, state, active):
        try:
            self.drain(cfg, active['worker'], f"rollout to {active['release']}")
        except WorkloadError as error:
            return self.abort(cfg, state, active, f'drain_refused:{error}', cooldown=900, release_drain=False)
        self.note('drained', worker=active['worker'])
        return self.goto(active, 'wait_idle')

    def idle(self, entries, worker):
        entry = self.entry_of(entries, worker)
        return bool(entry) and entry.get('connected') and not entry.get('running') and entry.get('state') != 'running'

    def keep_drained(self, cfg, entries, worker, reason):
        """A long-running job outlives a drain's expiry; renew ours before it lapses so no new work slips in."""
        claim = (self.entry_of(entries, worker) or {}).get('drain') or {}
        if claim.get('draining') and claim.get('owner') == OWNER and (claim.get('expires_in_s') is None or claim['expires_in_s'] > 900):
            return
        self.drain(cfg, worker, reason)
        self.note('drain_renewed', worker=worker)

    def step_wait_idle(self, cfg, status, entries, state, active):
        if self.idle(entries, active['worker']):
            return self.goto(active, 'switch')
        if self.clock() - active['step_started'] > cfg.get('idle_timeout_seconds', 1800):
            return self.abort(cfg, state, active, 'idle_timeout', cooldown=900)
        self.keep_drained(cfg, entries, active['worker'], f"rollout to {active['release']}")
        return 'wait'

    def step_switch(self, cfg, status, entries, state, active):
        if not self.idle(entries, active['worker']):
            return self.goto(active, 'wait_idle')   # a job slipped in; wait again, never restart over it
        args = dict(release=active['release'], content_hash=active['content_hash'])
        if cfg.get('releases_dir'):
            args['releases_dir'] = cfg['releases_dir']
        done = self.helper(active, 'switch', args)
        if not done.get('ok'):
            failure = done.get('failure')
            active['switched'] = failure not in PRE_SWITCH_FAILURES
            return self.fail(cfg, state, active, f'switch:{failure}', done.get('detail'))
        active['switched'] = True
        self.note('switched', worker=active['worker'], release=active['release'], previous=active['previous_release'])
        return self.goto(active, 'smoke')

    def step_smoke(self, cfg, status, entries, state, active):
        args = dict(release=active['release'], probes=active['probes'], window_s=cfg.get('restart_window_seconds', 45))
        if cfg.get('releases_dir'):
            args['releases_dir'] = cfg['releases_dir']
        done = self.helper(active, 'smoke', args, timeout=900)
        if not done.get('ok'):
            return self.fail(cfg, state, active, done.get('failure') or 'smoke_failed:unknown', None)
        self.note('smoke_passed', worker=active['worker'], probes=active['probes'])
        return self.goto(active, 'verify')

    def ready(self, entries, worker):
        entry = self.entry_of(entries, worker)
        return bool(entry) and entry.get('connected') and not entry.get('activation_failures') and (entry.get('last_seen_age_s') or 0) < 90

    def step_verify(self, cfg, status, entries, state, active):
        if self.ready(entries, active['worker']):
            return self.goto(active, 'enable')
        if self.clock() - active['step_started'] > cfg.get('verify_timeout_seconds', 180):
            return self.fail(cfg, state, active, 'verify:worker_not_ready_on_new_release', None)
        return 'wait'

    def step_enable(self, cfg, status, entries, state, active):
        self.enable(active['worker'], f"rollout to {active['release']} passed smoke")
        self.note('enabled', worker=active['worker'])
        return self.goto(active, 'soak')

    def step_soak(self, cfg, status, entries, state, active):
        worker = active['worker']
        entry = self.entry_of(entries, worker)
        if not entry or not entry.get('connected') or entry.get('activation_failures'):
            return self.fail(cfg, state, active, 'soak:worker_unhealthy_after_enable', None)
        here = self.helper(active, 'status', dict(worker=worker))
        if not here.get('ok') or here.get('active') != 'active' or here.get('content_hash') != active['content_hash']:
            return self.fail(cfg, state, active, 'soak:unit_not_on_the_declared_release_or_not_active', None)
        if self.clock() - active['step_started'] < active['soak_seconds']:
            return 'wait'
        state['failures'][active['group']] = 0
        state['active'] = None
        self.note('done', worker=worker, release=active['release'], previous=active['previous_release'])
        self.tell(cfg, f'rollout done: {worker} runs {active["release"]} (was {active["previous_release"]})')
        return 'done'

    # ---- unwinding ---------------------------------------------------------------------------

    def abort(self, cfg, state, active, reason, cooldown=0, release_drain=True, notify=False):
        """Nothing was changed on the host: give the worker back and try again later. Not a unit failure.
        `release_drain` is False when the drain is someone else's (never enable a worker we did not drain)."""
        if release_drain:
            try:
                self.enable(active['worker'], f'rollout aborted: {reason}')
            except WorkloadError as error:
                self.note('enable_failed', worker=active['worker'], error=str(error)[:120])
        if notify:
            self.tell(cfg, f"rollout of {active['release']} to {active['worker']} did not start: {reason}")
        state['cooldown'][active['worker']] = self.clock() + cooldown
        state['active'] = None
        self.note('aborted', worker=active['worker'], reason=reason)
        return 'done'

    def fail(self, cfg, state, active, reason, detail):
        active['failure'] = reason
        self.note('failed', worker=active['worker'], release=active['release'], step=active['step'], reason=reason,
                  detail=str(detail)[:200] if detail else None)
        if active['switched']:
            active['step'], active['step_started'] = 'rollback', self.clock()
            return 'continue'
        if active['step'] in ('stage', 'switch') or reason.startswith(('host:', 'authority:')):
            # nothing changed on the host and the cause is plumbing, not the release: give the worker back, retry later
            return self.abort(cfg, state, active, reason, cooldown=900, notify=True)
        return self.finish_failure(cfg, state, active)

    def step_unwind(self, cfg, status, entries, state, active):
        if active['switched']:
            return self.goto(active, 'rollback')
        return self.abort(cfg, state, active, active.get('unwind_reason', 'unwind'), cooldown=900)

    def step_rollback(self, cfg, status, entries, state, active):
        worker = active['worker']
        try:
            self.drain(cfg, worker, f"rollback of {active['release']}: {active.get('failure') or active.get('unwind_reason')}")
        except WorkloadError as error:
            if 'drain_held_by' in str(error):
                return self.rollback_failed(cfg, state, active, f'drain_held_by_other:{error}')
        if not self.idle(entries, worker):
            if self.clock() - active['step_started'] > cfg.get('idle_timeout_seconds', 1800):
                return self.rollback_failed(cfg, state, active, 'idle_timeout_before_rollback')
            self.keep_drained(cfg, entries, worker, f"rollback of {active['release']}")
            return 'wait'
        done = self.helper(active, 'rollback', dict(release=active['release']))
        if not done.get('ok'):
            return self.rollback_failed(cfg, state, active, done.get('failure'))
        self.note('rolled_back', worker=worker, to=active['previous_release'])
        return self.finish_failure(cfg, state, active)

    def finish_failure(self, cfg, state, active):
        worker, group = active['worker'], active['group']
        if active.get('unwind_reason'):   # a kill switch is not the release's fault
            try:
                self.enable(worker, 'rollout unwound by kill switch')
            except WorkloadError:
                pass
            state['active'] = None
            return 'done'
        try:
            self.enable(worker, f"rollout of {active['release']} rejected: {active.get('failure')}")
        except WorkloadError as error:
            self.note('enable_failed', worker=worker, error=str(error)[:120])
        rejected = state['rejected'].setdefault(worker, [])
        if active['release'] not in rejected:
            rejected.append(active['release'])
            del rejected[:-MAX_REJECTED]
        state['failures'][group] = state['failures'].get(group, 0) + 1
        paused = state['failures'][group] >= cfg.get('failure_budget', 2)
        if paused:
            state['paused'][group] = f"failures:{state['failures'][group]}"
        state['active'] = None
        message = (f"rollout FAILED: {worker} -> {active['release']}: {active.get('failure')}; rolled back, release rejected for this "
                   f"worker" + (f"; group {group} PAUSED after {state['failures'][group]} failures (clear state paused to resume)" if paused else ''))
        self.note('rejected', worker=worker, release=active['release'], failure=active.get('failure'), paused=paused)
        self.tell(cfg, message)
        return 'done'

    def rollback_failed(self, cfg, state, active, reason):
        worker = active['worker']
        try:
            self.drain(cfg, worker, f'ROLLBACK FAILED: {reason}', needs_operator=True)
        except WorkloadError:
            pass
        state['paused']['*'] = f'rollback_failed:{worker}:{reason}'
        state['active'] = None
        self.note('rollback_failed', worker=worker, release=active['release'], reason=reason)
        self.tell(cfg, f'ROLLOUT ROLLBACK FAILED on {worker}: {reason}. Worker left drained (needs_operator). All rollouts paused.')
        return 'done'
