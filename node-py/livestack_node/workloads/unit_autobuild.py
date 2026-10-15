"""Unit auto-build: landed means live for the e2e workers, without anyone building a unit by hand.

A deployment unit (unit.py) names the worker release a group of workers should run. Until now each unit was built
by hand after a runtime commit. This tool, run from a bounded user timer, does the same steps for ONE kind of
group (the e2e groups the config lists) and stops at a proposal:

    1. newest commit on origin/main that touches the runtime paths (node-py/livestack_node) = the candidate
    2. skip when the declared unit already has that runtime tree, when the candidate is younger than
       `min_commit_age_minutes`, when a unit was built less than `min_interval_hours` ago, or when this
       candidate already failed (retry only after `retry_after_hours`)
    3. GATE: the repo's own test command (`gate.tests`, default tests/test_workload*.py under node-py/) must pass
       on the candidate's tree; the worker release is built into `release_source` and the `release_import` smoke
       probe (the one the executor runs on the worker host) must PASS on it
    4. the new unit copies handlers/verifier/capture/min_authority from the currently declared unit (those are
       separate concerns, rolled by the handler registry) and carries the new release; it is registered with the
       authority, the rollout spec's set points at it, and the reconciler config's listed groups point at it
    5. a named notification says what was proposed. The rollout executor then rolls the workers one at a time
       (drain, idle, switch, smoke, verify, enable, soak, rollback on failure), exactly as for a hand-built unit.

Fail closed: any failed step raises a named notification (once per candidate), leaves the old declared unit in
place and changes nothing else. It never restarts, drains or rolls a worker back; it never touches a group that
`requires_approval` (signer/keystore workers) nor a worker in `never_workers` (the Mac, the legacy e2e-5): such a
group in the target list is a configuration error that stops the run.

State is one small JSON file (bounded history). Config keys: see docs/worker-claims-and-rollout.md,
"Unit auto-build". Kill switch: `enabled` false, the `kill_file`, or `systemctl --user disable --now
livestack-unit-autobuild.timer`.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .model import WorkloadError
from . import smoke
from . import unit as deployment_unit
from .host_release import content_hash as tree_content_hash

RUNTIME_PATHS = ('node-py/livestack_node',)
MAX_HISTORY = 20
MAX_FAILED = 20
DEFAULT_TESTS = ('tests/test_workload*.py',)


class Stop(Exception):
    """A named reason to stop this run. `notify` False = a quiet deferral, True = a failure to raise."""

    def __init__(self, stage, reason, notify=True):
        Exception.__init__(self, f'{stage}:{reason}')
        self.stage, self.reason, self.notify = stage, reason, notify


def expand(path):
    return Path(os.path.expanduser(str(path)))


def run(argv, cwd=None, timeout=120, env=None, stdin=None):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, env=env, input=stdin)
    return done.returncode, done.stdout.decode('utf-8', 'replace'), done.stderr.decode('utf-8', 'replace')


class State:
    def __init__(self, path):
        self.path = expand(path)

    def load(self):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            state = {}
        except ValueError:
            raise RuntimeError(f'unit auto-build state {self.path} is unreadable; refusing to guess')   # fail closed
        state.setdefault('failed', {}); state.setdefault('history', []); state.setdefault('last_built', 0)
        return state

    def save(self, state):
        state['history'] = state['history'][-MAX_HISTORY:]
        if len(state['failed']) > MAX_FAILED:
            for key in sorted(state['failed'], key=lambda k: state['failed'][k]['at'])[:-MAX_FAILED]:
                del state['failed'][key]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(state, sort_keys=True, indent=1))
        os.replace(tmp, self.path)


# ---- reading the world --------------------------------------------------------------------------------

class Repo:
    def __init__(self, path, fetch=True):
        self.path, self.fetch = expand(path), fetch

    def git(self, *args, timeout=120):
        code, out, err = run(['git', '-C', str(self.path), *args], timeout=timeout)
        if code != 0:
            raise Stop('git', f"{' '.join(args[:2])}: {err.strip()[-120:]}")
        return out.strip()

    def candidate(self):
        """(commit, committer epoch) of the newest origin/main commit touching the runtime paths."""
        if self.fetch:
            self.git('fetch', '-q', 'origin', timeout=180)
        out = self.git('log', '-1', '--format=%H %ct', 'origin/main', '--', *RUNTIME_PATHS)
        if not out:
            raise Stop('candidate', 'no_runtime_commit_on_origin_main')
        commit, when = out.split()
        return commit, int(when)

    def same_runtime(self, a, b):
        code, _, _ = run(['git', '-C', str(self.path), 'diff', '--quiet', a, b, '--', *RUNTIME_PATHS])
        return code == 0

    def resolve(self, short):
        return self.git('rev-parse', '--verify', short + '^{commit}')

    def count_between(self, base, tip):
        return int(self.git('rev-list', '--count', f'{base}..{tip}', '--', *RUNTIME_PATHS))

    def export(self, commit, dest):
        """Materialise node-py/, tools/ and LICENSE of `commit` into dest (no .git, nothing to clean up but the dir).

        tools/ is here because gate tests read repo-root files (tests/test_workload_supervised.py reads
        tools/supervised-handlers.json); without it the gate failed at collection on every commit."""
        code = subprocess.run(['git', '-C', str(self.path), 'archive', commit, '--', 'node-py', 'tools', 'LICENSE'],
                              capture_output=True, timeout=300)
        if code.returncode != 0:
            raise Stop('export', code.stderr.decode('utf-8', 'replace')[-120:])
        with tarfile.open(fileobj=io.BytesIO(code.stdout)) as tar:
            tar.extractall(dest, filter='data')


def authority_base_commit():
    """Commit of the release the local authority unit runs (user unit, same host), or None when unreadable."""
    code, out, _ = run(['systemctl', '--user', 'show', 'livestack-workload-authority.service', '-p', 'Environment'])
    for part in out.replace('Environment=', '').split():
        if part.startswith('PYTHONPATH='):
            for entry in part[len('PYTHONPATH='):].split(':'):
                if entry.endswith('/node-py'):
                    try:
                        return json.loads((Path(entry) / 'RELEASE.json').read_text())['commit']
                    except (OSError, ValueError, KeyError):
                        return None
    return None


# ---- the steps ----------------------------------------------------------------------------------------

def check_targets(rcfg, wanted, never):
    """The enforce groups this tool may re-point. Anything unsafe is a configuration error."""
    groups = [g for g in (rcfg.get('enforce') or {}).get('groups', []) if g.get('name') in wanted]
    missing = sorted(set(wanted) - {g['name'] for g in groups})
    if missing:
        raise Stop('config', f'target_group_not_in_reconciler_config:{",".join(missing)}')
    for group in groups:
        if group.get('requires_approval'):
            raise Stop('config', f"target_group_requires_approval:{group['name']} (signing workers stay with the operator)")
        bad = sorted(set(group.get('workers', [])) & set(never))
        if bad:
            raise Stop('config', f"target_group_has_manual_worker:{group['name']}:{','.join(bad)}")
    return groups


def declared_template(status, groups):
    sets = {g['set'] for g in groups}
    units = {(status.get('spec') or {}).get('sets', {}).get(s, {}).get('unit') for s in sets}
    if len(units) != 1 or None in units:
        raise Stop('declared', f'target_sets_do_not_share_one_declared_unit:{sorted(map(str, units))}')
    uid = units.pop()
    manifest = (status.get('manifests') or {}).get(uid)
    if manifest is None:
        raise Stop('declared', f'declared_unit_unknown_to_authority:{uid}')
    return uid, manifest


def run_gate(cfg, root):
    """The repo's own tests, on the candidate's tree. A pass is exit 0 with at least one test run."""
    gate = cfg.get('gate') or {}
    python = str(expand(gate['python'])) if gate.get('python') else sys.executable
    tests = gate.get('tests') or list(DEFAULT_TESTS)
    skipped = {f for pattern in gate.get('exclude', []) for f in glob.glob(str(Path(root) / 'node-py' / pattern))}
    files = sorted({f for pattern in tests for f in glob.glob(str(Path(root) / 'node-py' / pattern))} - skipped)
    if not files:
        raise Stop('gate', 'no_test_files_matched')
    argv = ['nice', '-n', '15', python, '-m', 'pytest', *[os.path.relpath(f, Path(root) / 'node-py') for f in files],
            '-q', '-p', 'no:cacheprovider', '--maxfail=5']
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    try:
        code, out, err = run(argv, cwd=str(Path(root) / 'node-py'), timeout=int(gate.get('timeout_seconds', 1800)), env=env)
    except subprocess.TimeoutExpired:
        raise Stop('gate', 'tests_timed_out')
    tail = [l for l in (out + err).strip().splitlines() if l.strip()]
    if code != 0:
        failed = [l.split(' ')[1] for l in tail if l.startswith('FAILED ')]
        if failed:
            # The gate runs niced beside the host's other work; a different timing-sensitive test failed on each of
            # four runs on 2026-10-15 and every one passed alone and in a full re-run. Run the failures once more by
            # themselves: a real failure fails again and keeps its name, a load flake does not block the candidate.
            try:
                code2, out2, err2 = run(argv[:6] + failed + ['-q', '-p', 'no:cacheprovider'], cwd=str(Path(root) / 'node-py'),
                                        timeout=int(gate.get('timeout_seconds', 1800)), env=env)
            except subprocess.TimeoutExpired:
                raise Stop('gate', 'tests_timed_out')
            if code2 == 0:
                logging.warning('gate: %d test(s) failed once and passed on a re-run: %s', len(failed), ','.join(failed))
                return f'{tail[-1] if tail else ""} (re-ran {len(failed)} failed test(s) once: passed)'
        raise Stop('gate', 'tests_failed:' + (','.join(failed[:3]) or (tail[-1] if tail else f'exit {code}'))[:160])
    return tail[-1] if tail else ''


def build_release(cfg, commit, source):
    script = expand(cfg.get('build_script') or Path(cfg['repo']) / 'node-py' / 'scripts' / 'build-worker-release.py')
    name = f'livestack-{commit[:8]}'
    out = source / name
    if out.exists():
        try:
            meta = json.loads((out / 'node-py' / 'RELEASE.json').read_text())
        except (OSError, ValueError):
            raise Stop('build', f'release_dir_exists_and_is_unreadable:{name}')
        if meta.get('commit') != commit:
            raise Stop('build', f'release_dir_exists_for_another_commit:{name}')
        # The unit carries RELEASE.json's hash and the host checks the pushed tree against it with this same
        # algorithm. A directory built by another tool (an authority release records a different hash) would stage
        # as staged_content_hash_mismatch on every worker (fc8b905a, 2026-10-13): refuse it here, by name.
        if meta.get('content_hash') != tree_content_hash(out):
            raise Stop('build', f'release_dir_exists_with_a_different_content_hash:{name}')
        return name, out
    code, _, err = run([sys.executable, str(script), 'build', commit, '--out', str(out)], timeout=300)
    if code != 0:
        shutil.rmtree(out, ignore_errors=True)
        raise Stop('build', err.strip()[-160:] or f'exit {code}')
    return name, out


def probe_release(cfg, out):
    probe = cfg.get('probe') or {}
    result = smoke.release_import(dict(release_root=str(out), python=str(expand(probe['python'])) if probe.get('python') else sys.executable,
                                       extra_path=[str(expand(p)) for p in probe.get('extra_path', [])]))
    if result['status'] != smoke.PASS:
        raise Stop('release_import', (result.get('failure') or result['status'])[:200])
    return result


def new_manifest(template, out, commit):
    meta = json.loads((out / 'node-py' / 'RELEASE.json').read_text())
    manifest = dict(version=deployment_unit.VERSION,
                    release=dict(name=f'livestack-{commit[:8]}', content_hash=meta['content_hash']),
                    handlers=template['handlers'], verifier=template['verifier'], capture=template['capture'],
                    min_authority=template['min_authority'], built_from=[commit[:12]])
    try:
        return deployment_unit.unit_id(manifest), manifest
    except WorkloadError as error:
        raise Stop('manifest', str(error))


def write_config_units(path, group_names, unit_id):
    """Point the named enforce groups at `unit_id`; nothing else in the file changes. Atomic, mode kept."""
    path = expand(path)
    mode = path.stat().st_mode & 0o777
    body = json.loads(path.read_text())
    for group in body['enforce']['groups']:
        if group['name'] in group_names:
            group['unit'] = unit_id
    tmp = path.with_name(path.name + '.autobuild.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as handle:
        json.dump(body, handle, indent=1)
    os.replace(tmp, path)


class AutoBuild:
    def __init__(self, cfg, client, repo, state, *, clock=time.time, notify=None, authority_commit=authority_base_commit):
        self.cfg, self.client, self.repo, self.state = cfg, client, repo, state
        self.clock, self.authority_commit = clock, authority_commit
        self.notify = notify or self.notify_cmd

    def notify_cmd(self, message):
        command = self.cfg.get('notify_cmd')
        if command:
            try:
                subprocess.run(['sh', '-c', command + ' "$1"', 'notify', message], capture_output=True, timeout=60)
            except Exception as error:   # a broken notifier must not hide the run's result
                logging.error('notify_failed: %s', type(error).__name__)

    def run_once(self, dry_run=False):
        """Returns a dict {outcome, ...}. Never raises for an expected stop."""
        cfg = self.cfg
        if not cfg.get('enabled', True) or (cfg.get('kill_file') and expand(cfg['kill_file']).exists()):
            return dict(outcome='disabled')
        state = self.state.load()
        state['current_candidate'] = 'config'   # the key a failure before a candidate is known is filed under
        try:
            return self._run(state, dry_run)
        except Stop as stop:
            if not stop.notify:
                return dict(outcome='deferred', stage=stop.stage, reason=stop.reason)
            key = state['current_candidate']
            first = key not in state['failed'] or state['failed'][key]['reason'] != str(stop)
            state['failed'][key] = dict(at=self.clock(), reason=str(stop), stage=stop.stage)
            self.state.save(state)
            if first and not dry_run:
                self.notify(f'Unit auto-build FAILED at {stop.stage} for {key[:8]}: {stop.reason}. The declared unit is unchanged; '
                            f'no worker was touched. Log: journalctl --user -u livestack-unit-autobuild')
            return dict(outcome='failed', stage=stop.stage, reason=stop.reason)

    def _run(self, state, dry_run):
        cfg = self.cfg
        rcfg_path = expand(cfg['reconciler_config'])
        rcfg = json.loads(rcfg_path.read_text())
        groups = check_targets(rcfg, cfg['groups'], cfg.get('never_workers', []))
        status = self.client.request('rollout')
        declared_id, template = declared_template(status, groups)
        commit, committed = self.repo.candidate()
        state['current_candidate'] = commit
        declared_commit = self.repo.resolve(template['release']['name'][len('livestack-'):])
        if self.repo.same_runtime(declared_commit, commit):
            return dict(outcome='up_to_date', declared=declared_id, candidate=commit[:8])
        now = self.clock()
        if now - committed < cfg.get('min_commit_age_minutes', 30) * 60:
            raise Stop('candidate', 'too_fresh', notify=False)
        wait = cfg.get('min_interval_hours', 6) * 3600 - (now - state['last_built'])
        if wait > 0:
            raise Stop('candidate', f'rate_limited:{int(wait // 60)}min_left', notify=False)
        prior = state['failed'].get(commit)
        if prior and now - prior['at'] < cfg.get('retry_after_hours', 12) * 3600:
            raise Stop('candidate', 'failed_recently_waiting_for_a_new_commit_or_the_retry_window', notify=False)
        lag_limit = cfg.get('authority_lag_limit')
        if lag_limit is not None:
            base = self.authority_commit()
            if not base:
                raise Stop('authority', 'running_authority_release_unreadable')
            lag = self.repo.count_between(base, commit)
            if lag > lag_limit:
                raise Stop('authority', f'authority_is_{lag}_runtime_commits_behind_the_candidate_(limit_{lag_limit}):_deploy_the_authority_first')
        if dry_run:
            return dict(outcome='would_build', declared=declared_id, candidate=commit[:8])
        scratch = Path(tempfile.mkdtemp(prefix='unit-autobuild-', dir=str(expand(cfg['state']).parent)))
        try:
            self.repo.export(commit, scratch)
            gate = run_gate(cfg, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        source = expand(cfg.get('release_source', '~/.local/share/livestack-workload-releases'))
        name, out = build_release(cfg, commit, source)
        probe_release(cfg, out)
        unit_id, manifest = new_manifest(template, out, commit)
        # register -> spec -> config. Each step is harmless alone: an unreferenced unit is inert, and the executor
        # only follows the CONFIG, which is written last.
        try:
            registered = self.client.request('rollout/units', dict(manifest=manifest))
            if registered.get('id', unit_id) != unit_id:
                raise Stop('register', f"authority_computed_another_id:{registered.get('id')}")
            spec = json.loads(json.dumps(status['spec']))
            for g in groups:
                spec['sets'][g['set']]['unit'] = unit_id
            self.client.request('rollout/spec', dict(spec=spec, if_generation=status['spec_generation']))
        except WorkloadError as error:
            raise Stop('register', str(error)[:160])
        try:
            write_config_units(rcfg_path, {g['name'] for g in groups}, unit_id)
        except (OSError, ValueError, KeyError) as error:
            raise Stop('propose', f'reconciler_config_not_written:{type(error).__name__}')
        state['last_built'] = now
        state['history'].append(dict(at=now, unit=unit_id, release=name, commit=commit, replaced=declared_id, gate=gate))
        state['failed'].pop(commit, None)
        self.state.save(state)
        self.notify(f'Unit auto-build: {unit_id} ({name}) passed the gate ({gate[:60]}) and the release_import probe, and is now the '
                    f'desired release of {", ".join(g["name"] for g in groups)}. The rollout executor rolls those workers one at a time '
                    f'(never mid-job, rollback on failure). Release/signing workers and the Mac still wait for the operator. '
                    f'It replaced {declared_id}.')
        return dict(outcome='proposed', unit=unit_id, release=name, replaced=declared_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true', help='decide and report; build, register and write nothing')
    args = parser.parse_args(argv)
    cfg = json.loads(expand(args.config).read_text())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    from .client import WorkloadClient
    rcfg = json.loads(expand(cfg['reconciler_config']).read_text())   # the rollout principal's credential: never printed
    client = WorkloadClient(rcfg['authority'], rcfg['token'], timeout=60)
    auto = AutoBuild(cfg, client, Repo(cfg['repo']), State(cfg['state']))
    result = auto.run_once(dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 1 if result['outcome'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
