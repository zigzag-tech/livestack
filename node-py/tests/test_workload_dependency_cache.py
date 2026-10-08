"""Dependency cache: real directories, real cp, real flock. No dockerd is needed."""
import json
import os
from pathlib import Path

import pytest

from livestack_node.workloads import dependency_cache as dc


def config(tmp_path, **over):
    raw = dict(enabled=True, path=str(tmp_path/'cache'), refresh_every=0)
    raw.update(over)
    value, why = dc.settings(raw)
    assert why == 'enabled', why
    (tmp_path/'cache').mkdir(mode=0o700, exist_ok=True)
    return value


def make_source(tmp_path, name='src', lock='lock-1'):
    source = tmp_path/name
    (source/'.livestack').mkdir(parents=True)
    (source/'.livestack/dependency-cache.json').write_text(json.dumps(dict(version=1, components=[
        dict(path='{}/node_modules', for_each=['app', 'pkgs/*'], key_paths=['{}/package.json', '{}/package-lock.json'])])))
    for root in ('app', 'pkgs/a', 'pkgs/b'):
        (source/root).mkdir(parents=True)
        (source/root/'package.json').write_text('{}')
        (source/root/'package-lock.json').write_text(lock + root)
    return source


def install(source, root, content='x'):
    """What a handler's npm ci leaves behind: files, an executable, a relative link."""
    modules = source/root/'node_modules'
    (modules/'dep/bin').mkdir(parents=True)
    (modules/'dep/index.js').write_text(content)
    (modules/'dep/bin/run').write_text('#!/bin/sh\n')
    (modules/'dep/bin/run').chmod(0o755)
    (modules/'.bin').mkdir()
    os.symlink('../dep/bin/run', modules/'.bin/run')


def run_handler(source, output, roots):
    for root in roots:
        install(source, root)
    output.mkdir(exist_ok=True)
    (output/dc.COMMIT_FILE).write_text(json.dumps(dict(version=1, paths=[r + '/node_modules' for r in roots])))


ROOTS = ('app', 'pkgs/a', 'pkgs/b')


def attempt(cfg, name='a1', owner='principal-1'):
    return dc.Attempt(cfg, owner, name, '/bin/sh')


def test_settings_reject_unknown_relative_and_bad_numbers(tmp_path):
    assert dc.settings(None)[1] == 'not-configured'
    assert dc.settings(dict(enabled=False))[1] == 'disabled'
    assert dc.settings(dict(enabled=True, path='rel'))[1].startswith('invalid')
    assert dc.settings(dict(enabled=True, path='/x', bogus=1))[1].startswith('invalid: unknown')
    assert dc.settings(dict(enabled=True, path='/x', max_bytes=1))[1].startswith('invalid')


def test_miss_then_save_then_hit_restores_identical_tree(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = attempt(cfg, 'a1')
    assert {r['outcome'] for r in cold.restore(first)} == {'miss'}
    assert json.loads(cold.env()) == []
    run_handler(first, tmp_path/'out1', ROOTS)
    saved = cold.save(first, tmp_path/'out1')
    assert [r['outcome'] for r in saved] == ['saved'] * 3

    second = make_source(tmp_path, 'two')
    warm = attempt(cfg, 'a2')
    records = warm.restore(second)
    assert [r['outcome'] for r in records] == ['reused'] * 3
    assert sorted(item['path'] for item in json.loads(warm.env())) == sorted(r + '/node_modules' for r in ROOTS)
    for root in ROOTS:
        assert (second/root/'node_modules/dep/index.js').read_text() == 'x'
        assert os.readlink(second/root/'node_modules/.bin/run') == '../dep/bin/run'
        assert os.access(second/root/'node_modules/dep/bin/run', os.X_OK)
    # a restored tree is a copy: writing it never reaches the store
    (second/'app/node_modules/dep/index.js').write_text('changed')
    third = make_source(tmp_path, 'three')
    attempt(cfg, 'a3').restore(third)
    assert (third/'app/node_modules/dep/index.js').read_text() == 'x'
    assert warm.save(second, tmp_path/'out2') == []     # nothing missed, nothing stored


def test_changed_lockfile_is_a_different_key(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = attempt(cfg)
    cold.restore(first)
    run_handler(first, tmp_path/'o', ROOTS)
    cold.save(first, tmp_path/'o')
    changed = make_source(tmp_path, 'two', lock='lock-2')
    assert {r['outcome'] for r in attempt(cfg, 'a2').restore(changed)} == {'miss'}
    assert not (changed/'app/node_modules').exists()


def test_principals_never_share_entries(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = attempt(cfg, 'a1', 'alice')
    cold.restore(first)
    run_handler(first, tmp_path/'o', ROOTS)
    cold.save(first, tmp_path/'o')
    other = make_source(tmp_path, 'two')
    assert {r['outcome'] for r in attempt(cfg, 'a2', 'bob').restore(other)} == {'miss'}


def test_toolchain_change_is_a_different_key(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = dc.Attempt(cfg, 'p', 'a1', '/bin/sh')
    cold.restore(first)
    run_handler(first, tmp_path/'o', ROOTS)
    cold.save(first, tmp_path/'o')
    other = make_source(tmp_path, 'two')
    assert {r['outcome'] for r in dc.Attempt(cfg, 'p', 'a2', '/bin/cat').restore(other)} == {'miss'}


def test_nothing_is_saved_without_a_commit_or_for_uncommitted_paths(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = attempt(cfg)
    cold.restore(first)
    for root in ROOTS:
        install(first, root)
    (tmp_path/'o').mkdir()
    assert {r['reason'] for r in cold.save(first, tmp_path/'o')} == {'handler-wrote-no-commit'}
    (tmp_path/'o'/dc.COMMIT_FILE).write_text(json.dumps(dict(version=1, paths=['app/node_modules'])))
    outcomes = {r['path']: r['outcome'] for r in cold.save(first, tmp_path/'o')}
    assert outcomes == {'app/node_modules': 'saved', 'pkgs/a/node_modules': 'not-saved', 'pkgs/b/node_modules': 'not-saved'}


def test_unsafe_trees_are_not_saved(tmp_path):
    cfg = config(tmp_path)
    for kind in ('absolute', 'escape'):
        source = make_source(tmp_path, 'src-' + kind, lock=kind)
        cold = attempt(cfg, kind)
        cold.restore(source)
        run_handler(source, tmp_path/('o' + kind), ROOTS)
        target = '/etc/passwd' if kind == 'absolute' else '../../../../../../outside'
        os.symlink(target, source/'app/node_modules/bad')
        reasons = {r['path']: r.get('reason') for r in cold.save(source, tmp_path/('o' + kind))}
        assert reasons['app/node_modules'] in ('absolute-symlink', 'symlink-escapes-source')


def test_key_changed_during_the_attempt_is_not_saved(tmp_path):
    cfg = config(tmp_path)
    source = make_source(tmp_path)
    cold = attempt(cfg)
    cold.restore(source)
    run_handler(source, tmp_path/'o', ROOTS)
    (source/'app/package-lock.json').write_text('rewritten by the run')
    reasons = {r['path']: r['outcome'] + ':' + r.get('reason', '') for r in cold.save(source, tmp_path/'o')}
    assert reasons['app/node_modules'] == 'not-saved:key-changed'


def test_damaged_entry_is_dropped_and_treated_as_a_miss(tmp_path):
    cfg = config(tmp_path)
    first = make_source(tmp_path, 'one')
    cold = attempt(cfg)
    cold.restore(first)
    run_handler(first, tmp_path/'o', ROOTS)
    cold.save(first, tmp_path/'o')
    victim = next((Path(cfg['path'])).glob('*/entries/*/data/dep/index.js'))
    victim.unlink()        # structural damage (a lost file) is detected; same-size edits are bounded by refresh_every
    second = make_source(tmp_path, 'two')
    records = attempt(cfg, 'a2').restore(second)
    assert sorted(r['outcome'] for r in records) == ['miss', 'reused', 'reused']
    assert any(r['reason'].startswith('verify-failed') for r in records if r['outcome'] == 'miss')
    third = make_source(tmp_path, 'three')
    assert sorted(r['outcome'] for r in attempt(cfg, 'a3').restore(third)) == ['miss', 'reused', 'reused']


def test_existing_destination_and_missing_key_file_are_skipped(tmp_path):
    cfg = config(tmp_path)
    source = make_source(tmp_path)
    (source/'app/node_modules').mkdir()
    (source/'pkgs/a/package-lock.json').unlink()
    records = {r['path']: r['reason'] for r in attempt(cfg).restore(source)}
    assert records['app/node_modules'] == 'destination-exists'
    assert records['pkgs/a/node_modules'] == 'no-key'


def test_scheduled_refresh_runs_cold_and_replaces_the_entry(tmp_path):
    cfg = config(tmp_path, refresh_every=1)          # every attempt is a refresh
    first = make_source(tmp_path, 'one')
    one = attempt(cfg, 'a1')
    assert {r['outcome'] for r in one.restore(first)} == {'refresh'}
    run_handler(first, tmp_path/'o', ROOTS)
    assert {r['outcome'] for r in one.save(first, tmp_path/'o')} == {'saved'}
    second = make_source(tmp_path, 'two')
    two = attempt(cfg, 'a2')
    assert {r['outcome'] for r in two.restore(second)} == {'refresh'}
    assert json.loads(two.env()) == []
    for root in ROOTS:
        install(second, root, content='fresh')
    (tmp_path/'o2').mkdir()
    (tmp_path/'o2'/dc.COMMIT_FILE).write_text(json.dumps(dict(version=1, paths=[r + '/node_modules' for r in ROOTS])))
    assert {r['outcome'] for r in two.save(second, tmp_path/'o2')} == {'saved'}
    entries = list(Path(cfg['path']).glob('*/entries/*/data/dep/index.js'))
    assert len(entries) == 3 and {e.read_text() for e in entries} == {'fresh'}


def test_least_recently_used_entry_is_evicted_to_stay_under_the_bound(tmp_path):
    cfg = config(tmp_path, max_bytes=64*1024**2)
    cfg['max_bytes'] = 3 * 8192 + 100          # an entry here is two 4 KiB files: room for three
    sources = []
    for index in range(6):
        source = make_source(tmp_path, 's%d' % index, lock='lock-%d' % index)
        cache = attempt(cfg, 'a%d' % index)
        cache.restore(source)
        run_handler(source, tmp_path/('o%d' % index), ROOTS[:1])
        cache.save(source, tmp_path/('o%d' % index))
        sources.append(source)
    entries = list(Path(cfg['path']).glob('*/entries/*/meta.json'))
    total = sum(json.loads(m.read_text())['bytes'] for m in entries)
    assert 0 < len(entries) <= 3 and total <= cfg['max_bytes']
    # the newest entry survives
    assert {r['outcome'] for r in attempt(cfg, 'z').restore(make_source(tmp_path, 'again', lock='lock-5'))} >= {'reused'}


def test_manifest_validation_is_closed(tmp_path):
    source = tmp_path/'s'
    (source/'.livestack').mkdir(parents=True)
    bad = [
        dict(version=2, components=[]),
        dict(version=1, components=[dict(path='/abs', key_paths=['a'])]),
        dict(version=1, components=[dict(path='../up', key_paths=['a'])]),
        dict(version=1, components=[dict(path='{}/m', key_paths=['a'])]),                     # {} without for_each
        dict(version=1, components=[dict(path='m', for_each=['x'], key_paths=['a'])]),        # for_each without {}
        dict(version=1, components=[dict(path='m', key_paths=['a'], extra=1)]),
    ]
    for manifest in bad:
        (source/'.livestack/dependency-cache.json').write_text(json.dumps(manifest))
        components, reason = dc.load_manifest(source)
        assert components == [] and reason.startswith('invalid-manifest'), (manifest, reason)
    assert dc.load_manifest(tmp_path/'none')[1] == 'no-manifest'


def test_no_manifest_or_unsupported_pattern_means_cold_with_a_named_reason(tmp_path):
    cfg = config(tmp_path)
    plain = tmp_path/'plain'
    plain.mkdir()
    assert attempt(cfg).restore(plain) == [dict(outcome='skipped', reason='no-manifest')]
    source = make_source(tmp_path)
    manifest = json.loads((source/'.livestack/dependency-cache.json').read_text())
    manifest['components'][0]['for_each'] = ['*/deep']
    (source/'.livestack/dependency-cache.json').write_text(json.dumps(manifest))
    assert attempt(cfg).restore(source)[0]['reason'].startswith('unsupported-pattern')


# ------------------------------------------------------------ per-handler opt-in

def worker_stub(cfg):
    from types import SimpleNamespace
    return SimpleNamespace(dependency_cache=cfg)


def attempt_for(cfg, handler):
    from livestack_node.workloads.worker import WorkloadWorker as Worker
    assignment = dict(owner='principal-1', spec=dict(handler='h'))
    return Worker._dependency_attempt(worker_stub(cfg), assignment, 'a1', handler)


@pytest.mark.parametrize('handler', [
    dict(argv=['/bin/sh']),                                   # never opted in
    dict(argv=['/bin/sh'], dependency_cache=False),
    dict(argv=['/bin/sh'], dependency_cache='true'),          # not the boolean: fail closed
    dict(argv=['/bin/sh'], dependency_cache=1),
])
def test_a_handler_that_has_not_opted_in_gets_no_restore_and_no_store(tmp_path, handler):
    cfg = config(tmp_path)
    source = make_source(tmp_path)
    # a warm entry exists for this very source, put there by an opted-in handler
    warm = attempt_for(cfg, dict(argv=['/bin/sh'], dependency_cache=True))
    warm.restore(source)
    run_handler(source, tmp_path/'o', ROOTS)
    assert {r['outcome'] for r in warm.save(source, tmp_path/'o')} == {'saved'}
    entries_before = sorted(p.name for p in Path(cfg['path']).glob('*/entries/*'))

    other = make_source(tmp_path, 'other')
    assert attempt_for(cfg, handler) is None                  # the worker never builds an Attempt for it
    assert not list(other.glob('*/node_modules')) and not list(other.glob('pkgs/*/node_modules'))
    assert sorted(p.name for p in Path(cfg['path']).glob('*/entries/*')) == entries_before


def test_an_opted_in_handler_gets_an_attempt_and_a_disabled_worker_gets_none(tmp_path):
    cfg = config(tmp_path)
    assert attempt_for(cfg, dict(argv=['/bin/sh'], dependency_cache=True)) is not None
    assert attempt_for(None, dict(argv=['/bin/sh'], dependency_cache=True)) is None
    assert dc.opted_in(dict(dependency_cache=True)) == (True, None)
    assert dc.opted_in(dict(dependency_cache='yes'))[1].startswith('invalid')
