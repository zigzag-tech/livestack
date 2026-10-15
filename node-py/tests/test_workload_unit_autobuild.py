"""Unit auto-build: a real throwaway git repo, the real build script and the real release_import probe; a fake
authority client. Covers the proposal path, every fail-closed stop and the groups it must never touch."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from livestack_node.workloads import unit_autobuild as ua

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build-worker-release.py'
HANDLER = 'a' * 64


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                                   GIT_COMMITTER_EMAIL='t@t')).stdout.strip()


def commit(repo, files, message='c', when=None):
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(repo, 'add', '-A')
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    if when:
        env['GIT_COMMITTER_DATE'] = env['GIT_AUTHOR_DATE'] = f'@{int(when)} +0000'
    subprocess.run(['git', '-C', str(repo), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', message], check=True, env=env)
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/main', sha)
    return sha


GOOD_TEST = 'def test_ok():\n    assert True\n'
BAD_TEST = 'def test_bad():\n    assert False\n'


class Client:
    def __init__(self, status):
        self.status, self.calls = status, []

    def request(self, route, body=None):
        self.calls.append((route, body))
        if route == 'rollout':
            return self.status
        if route == 'rollout/units':
            return {'id': ua.deployment_unit.unit_id(body['manifest'])}
        return {'ok': True}


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'node-py' / 'scripts').mkdir(parents=True)
    shutil.copy(SCRIPT, repo / 'node-py' / 'scripts' / 'build-worker-release.py')
    base = commit(repo, {'LICENSE': 'x', 'tools/supervised-handlers.json': '{}', 'node-py/livestack_node/__init__.py': '',
                         'node-py/livestack_node/m.py': 'def f():\n    return 1\n',
                         'node-py/tests/test_ok.py': GOOD_TEST}, when=1000)
    source = tmp_path / 'releases'
    source.mkdir()
    build = subprocess.run([sys.executable, str(repo / 'node-py/scripts/build-worker-release.py'), 'build', base,
                            '--out', str(source / f'livestack-{base[:8]}')], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    meta = json.loads((source / f'livestack-{base[:8]}/node-py/RELEASE.json').read_text())
    manifest = dict(version='deployment-unit.v1', release=dict(name=f'livestack-{base[:8]}', content_hash=meta['content_hash']),
                    handlers=[HANDLER], verifier=None, capture=dict(size_bytes=0, cap_bytes=100),
                    min_authority='main', built_from=[base[:12]])
    old_id = ua.deployment_unit.unit_id(manifest)
    status = dict(manifests={old_id: manifest}, spec_generation=7,
                  spec=dict(version='rollout-spec.v1', mode='observe', workers={},
                            sets={'e2e': dict(unit=old_id, min_claiming=1, selector=dict(id_prefix='e2e-')),
                                  'rel': dict(min_claiming=1, selector=dict(ids=['rel-1']))}))
    rcfg = tmp_path / 'reconciler.json'
    rcfg.write_text(json.dumps(dict(authority='http://127.0.0.1:1', token='t' * 40, enforce=dict(groups=[
        dict(name='e2e-canary', set='e2e', unit=old_id, workers=['e2e-3']),
        dict(name='e2e', set='e2e', unit=old_id, workers=['e2e-1', 'e2e-2']),
        dict(name='rel', set='rel', unit=old_id, workers=['rel-1'], requires_approval=True)]))))
    os.chmod(rcfg, 0o600)
    cfg = dict(repo=str(repo), reconciler_config=str(rcfg), state=str(tmp_path / 'state.json'), release_source=str(source),
               groups=['e2e-canary', 'e2e'], never_workers=['e2e-5'], min_commit_age_minutes=30, min_interval_hours=6,
               retry_after_hours=12, gate=dict(python=sys.executable, tests=['tests/test_ok.py'], timeout_seconds=120),
               probe=dict(python=sys.executable), build_script=str(repo / 'node-py/scripts/build-worker-release.py'))
    sent = []
    clock = [10 ** 7]
    client = Client(status)
    auto = ua.AutoBuild(cfg, client, ua.Repo(repo, fetch=False), ua.State(cfg['state']), clock=lambda: clock[0],
                        notify=sent.append, authority_commit=lambda: base)
    return dict(repo=repo, base=base, auto=auto, sent=sent, client=client, cfg=cfg, rcfg=rcfg, old_id=old_id, clock=clock,
                source=source)


def runtime_commit(w, body='def f():\n    return 2\n', tests=GOOD_TEST):
    return commit(w['repo'], {'node-py/livestack_node/m.py': body, 'node-py/tests/test_ok.py': tests}, when=2000)


def groups_units(w):
    return {g['name']: g['unit'] for g in json.loads(w['rcfg'].read_text())['enforce']['groups']}


def test_up_to_date_when_the_declared_unit_has_the_candidates_runtime_tree(world):
    commit(world['repo'], {'docs/readme.md': 'doc only'}, when=2000)   # not a runtime path
    assert world['auto'].run_once()['outcome'] == 'up_to_date'
    assert world['sent'] == []


def test_proposes_a_unit_for_the_e2e_groups_only_and_keeps_the_config_secret_file_private(world):
    sha = runtime_commit(world)
    result = world['auto'].run_once()
    assert result['outcome'] == 'proposed', result
    new = result['unit']
    units = groups_units(world)
    assert units == {'e2e-canary': new, 'e2e': new, 'rel': world['old_id']}, 'the approval-gated group keeps its unit'
    routes = [r for r, _ in world['client'].calls]
    assert routes == ['rollout', 'rollout/units', 'rollout/spec']
    spec_call = world['client'].calls[2][1]
    assert spec_call['if_generation'] == 7 and spec_call['spec']['sets']['e2e']['unit'] == new
    assert 'unit' not in spec_call['spec']['sets']['rel']
    manifest = world['client'].calls[1][1]['manifest']
    assert manifest['release']['name'] == f'livestack-{sha[:8]}' and manifest['handlers'] == [HANDLER] and manifest['built_from'] == [sha[:12]]
    assert (world['source'] / f'livestack-{sha[:8]}').is_dir()
    assert stat.S_IMODE(world['rcfg'].stat().st_mode) == 0o600
    assert json.loads(world['rcfg'].read_text())['token'] == 't' * 40
    assert len(world['sent']) == 1 and new in world['sent'][0] and 'e2e-canary' in world['sent'][0]
    assert 't' * 40 not in world['sent'][0]


def test_a_failing_gate_keeps_the_old_unit_notifies_once_and_is_not_retried_at_once(world):
    sha = runtime_commit(world, tests=BAD_TEST)
    result = world['auto'].run_once()
    assert (result['outcome'], result['stage']) == ('failed', 'gate')
    assert groups_units(world)['e2e'] == world['old_id']
    assert [r for r, _ in world['client'].calls] == ['rollout'], 'nothing registered'
    assert not (world['source'] / f'livestack-{sha[:8]}').exists(), 'no release built for a candidate that failed the gate'
    assert len(world['sent']) == 1 and 'FAILED at gate' in world['sent'][0] and sha[:8] in world['sent'][0]
    world['clock'][0] += 3600
    assert world['auto'].run_once()['outcome'] == 'deferred'
    assert len(world['sent']) == 1
    world['clock'][0] += 13 * 3600
    assert world['auto'].run_once()['outcome'] == 'failed'
    assert len(world['sent']) == 1, 'the same failure is not announced twice'


def test_release_import_failure_stops_before_anything_is_declared(world):
    runtime_commit(world, body='def f():\n    return nowhere_defined\n')
    result = world['auto'].run_once()
    assert (result['outcome'], result['stage']) == ('failed', 'release_import')
    assert 'nowhere_defined' in result['reason']
    assert groups_units(world)['e2e'] == world['old_id']
    assert 'rollout/units' not in [r for r, _ in world['client'].calls]


def test_deferrals_are_quiet_too_fresh_and_rate_limited(world):
    runtime_commit(world)
    world['clock'][0] = 2000 + 10 * 60
    assert world['auto'].run_once() == dict(outcome='deferred', stage='candidate', reason='too_fresh')
    world['clock'][0] = 10 ** 7
    assert world['auto'].run_once()['outcome'] == 'proposed'
    runtime_commit(world, body='def f():\n    return 3\n')
    world['clock'][0] += 3600
    again = world['auto'].run_once()
    assert again['outcome'] == 'deferred' and again['reason'].startswith('rate_limited')
    world['clock'][0] += 6 * 3600
    assert world['auto'].run_once()['outcome'] == 'proposed'


@pytest.mark.parametrize('mutate,needle', [
    (lambda g: g.update(requires_approval=True), 'target_group_requires_approval:e2e'),
    (lambda g: g['workers'].append('e2e-5'), 'target_group_has_manual_worker:e2e:e2e-5'),
])
def test_a_signing_or_manual_worker_in_a_target_group_is_a_config_error_that_changes_nothing(world, mutate, needle):
    runtime_commit(world)
    body = json.loads(world['rcfg'].read_text())
    mutate(next(g for g in body['enforce']['groups'] if g['name'] == 'e2e'))
    world['rcfg'].write_text(json.dumps(body))
    result = world['auto'].run_once()
    assert result['outcome'] == 'failed' and needle in result['reason']
    assert world['client'].calls == []
    assert groups_units(world)['e2e'] == world['old_id']


def test_authority_too_far_behind_the_candidate_is_named_and_nothing_is_built(world):
    world['cfg']['authority_lag_limit'] = 0
    runtime_commit(world)
    result = world['auto'].run_once()
    assert (result['outcome'], result['stage']) == ('failed', 'authority')
    assert 'deploy_the_authority_first' in result['reason']


def test_kill_switch_and_dry_run_do_nothing(world):
    runtime_commit(world)
    world['cfg']['enabled'] = False
    assert world['auto'].run_once()['outcome'] == 'disabled'
    world['cfg']['enabled'] = True
    assert world['auto'].run_once(dry_run=True)['outcome'] == 'would_build'
    assert groups_units(world)['e2e'] == world['old_id'] and world['sent'] == []


def test_a_test_that_fails_once_and_passes_on_its_own_re_run_does_not_block_the_candidate(world, tmp_path):
    marker = tmp_path / 'ran-once'
    flaky = ('import os\n\n\ndef test_load_flake():\n    m = %r\n    first = not os.path.exists(m)\n'
             '    open(m, "w").close()\n    assert not first\n') % str(marker)
    runtime_commit(world, tests=flaky)
    assert world['auto'].run_once()['outcome'] == 'proposed'
    assert marker.exists()


def test_the_gate_tree_carries_repo_root_tools(world, tmp_path):
    # tests/test_workload_supervised.py reads tools/supervised-handlers.json; a gate tree without tools/ failed at
    # collection ("1 error") on every commit and no new unit was ever proposed.
    dest = tmp_path / 'gate-tree'
    dest.mkdir()
    ua.Repo(world['repo'], fetch=False).export(world['base'], dest)
    assert (dest / 'tools' / 'supervised-handlers.json').is_file()


def test_a_prebuilt_release_dir_whose_recorded_hash_is_not_the_trees_is_refused(world):
    # fc8b905a: tools/build-authority-release.sh had already made the directory with its own hash scheme; the unit
    # copied that hash and every worker refused the staged tree (staged_content_hash_mismatch).
    sha = runtime_commit(world)
    rel = world['source'] / f'livestack-{sha[:8]}'
    subprocess.run([sys.executable, str(world['repo'] / 'node-py/scripts/build-worker-release.py'), 'build', sha, '--out', str(rel)],
                   check=True, capture_output=True)
    meta_path = rel / 'node-py' / 'RELEASE.json'
    meta = json.loads(meta_path.read_text())
    meta['content_hash'] = '0' * 64
    meta_path.write_text(json.dumps(meta))
    result = world['auto'].run_once()
    assert result['outcome'] == 'failed' and 'different_content_hash' in json.dumps(result)
    assert groups_units(world)['e2e'] == world['old_id']


def test_excluded_test_files_do_not_gate(world):
    commit(world['repo'], {'node-py/livestack_node/m.py': 'def f():\n    return 2\n', 'node-py/tests/test_env_bound.py': BAD_TEST}, when=2000)
    world['cfg']['gate']['tests'] = ['tests/test_*.py']
    assert world['auto'].run_once()['outcome'] == 'failed'
    world['cfg']['gate']['exclude'] = ['tests/test_env_bound.py']
    world['clock'][0] += 13 * 3600
    assert world['auto'].run_once()['outcome'] == 'proposed'
