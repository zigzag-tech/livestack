"""Supervised handlers (openspec attempt-progress-reporting): one spec, two implementations.

* the shared vectors.json reproduce byte for byte in Python and in Node (no drift);
* the conformance scan fails handler modules that reach raw process/network/sleep primitives;
* every enrolled handler (tools/supervised-handlers.json) runs under a fake clock and proves it emits
  within its declared max_silence."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from livestack_node.workloads import lease_helper
from livestack_node.workloads.model import WorkloadError, progress as validate_progress
from livestack_node.workloads.supervised import supervised as sv
from livestack_node.workloads.supervised.simulate import SilenceBreach, prove

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
VECTORS = json.loads((ROOT/'node-py/livestack_node/workloads/supervised/vectors.json').read_text())
FIXTURES = HERE/'fixtures/supervised'
NODE = shutil.which('node')


def run_vector(vector):
    state = sv.init(vector['contract'], 0)
    emits = []
    for event in vector['events']:
        doc = sv.step(state, event)
        if doc is not None:
            emits.append({'t': event['t'], 'doc': doc})
    return {'name': vector['name'], 'emits': emits, 'dropped': state['dropped'],
            'verdicts': [{'t': v['t'], 'state': sv.verdict(state, v['t'])} for v in vector['expect']['verdicts']]}


@pytest.mark.parametrize('vector', VECTORS, ids=[v['name'][:50] for v in VECTORS])
def test_python_reducer_reproduces_the_shared_vectors(vector):
    got = run_vector(vector)
    assert got['emits'] == vector['expect']['emits']
    assert got['dropped'] == vector['expect']['dropped']
    assert got['verdicts'] == vector['expect']['verdicts']
    for emitted in got['emits']:
        validate_progress(emitted['doc'])  # every emitted document is one the authority accepts


def test_node_reducer_gives_the_identical_result_as_python():
    if NODE is None:
        pytest.skip('requires node')
    out = subprocess.run([NODE, str(HERE/'supervised_vectors.mjs')], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [run_vector(v) for v in VECTORS], 'Python and Node reducers drifted'


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conformance_scan_passes_the_repo_config_and_catches_raw_primitives(tmp_path):
    checker = ROOT/'tools/check-supervised-handlers.py'
    assert subprocess.run([sys.executable, str(checker)], capture_output=True, text=True).returncode == 0
    (tmp_path/'tools').mkdir()
    (tmp_path/'tools/supervised-handlers.json').write_text(json.dumps(
        {'handlers': ['bad_*'], 'allowlist': [], 'max_allowlist': 0}))
    for source in FIXTURES.glob('bad_*'):
        shutil.copy(source, tmp_path/source.name)
    run = subprocess.run([sys.executable, str(checker), str(tmp_path/'tools/supervised-handlers.json')],
                         capture_output=True, text=True)
    assert run.returncode == 1
    for needle in ('import subprocess', 'time.sleep', 'child_process', 'fetch(', 'setTimeout(',
                   'bad_unsupervised.py: handler module does not use supervised('):
        assert needle in run.stdout, run.stdout


def test_allowlist_is_a_ratchet(tmp_path):
    checker = ROOT/'tools/check-supervised-handlers.py'
    (tmp_path/'tools').mkdir()
    shutil.copy(FIXTURES/'bad_raw.py', tmp_path/'bad_raw.py')
    shutil.copy(FIXTURES/'good_handler.py', tmp_path/'good_handler.py')

    def scan(**config):
        (tmp_path/'tools/supervised-handlers.json').write_text(json.dumps(dict(handlers=['*.py'], **config)))
        return subprocess.run([sys.executable, str(checker), str(tmp_path/'tools/supervised-handlers.json')],
                              capture_output=True, text=True)
    assert scan(allowlist=['bad_raw.py'], max_allowlist=1).returncode == 0, 'a recorded violation passes'
    assert 'may only shrink' in scan(allowlist=['bad_raw.py', 'good_handler.py'], max_allowlist=1).stdout
    stale = scan(allowlist=['bad_raw.py', 'good_handler.py'], max_allowlist=2)
    assert 'allowlisted but clean' in stale.stdout, 'an entry that no longer violates must be removed'
    assert scan(allowlist=[], max_allowlist=0).returncode == 1, 'unlisted violations fail'


def enrolled(suffix):
    config = json.loads((ROOT/'tools/supervised-handlers.json').read_text())
    import glob
    return sorted(Path(p) for pattern in config['handlers'] for p in glob.glob(str(ROOT/pattern), recursive=True)
                  if p.endswith(suffix))


@pytest.mark.parametrize('path', enrolled('.py'), ids=lambda p: p.name)
def test_every_enrolled_python_handler_emits_within_its_max_silence_under_a_fake_clock(path):
    module = load(path)
    emitted, _ = prove(module.handler, module.SCENARIO)
    assert emitted[0]['phase'] == 'prepare'
    assert emitted[-1].get('unit') == 'targets' or 'beat' in emitted[-1]
    for doc in emitted:
        validate_progress(doc)


@pytest.mark.parametrize('path', enrolled('.mjs'), ids=lambda p: p.name)
def test_every_enrolled_node_handler_emits_within_its_max_silence_under_a_fake_clock(path):
    if NODE is None:
        pytest.skip('requires node')
    out = subprocess.run([NODE, str(FIXTURES/'run_proof.mjs'), str(path)], capture_output=True, text=True, timeout=30)
    assert json.loads(out.stdout)['ok'], out.stdout + out.stderr


def test_a_handler_with_one_long_silent_step_fails_the_proof_in_both_languages():
    module = load(FIXTURES/'silent_handler.py')
    with pytest.raises(SilenceBreach):
        prove(module.handler, module.SCENARIO)
    if NODE is not None:
        out = subprocess.run([NODE, str(FIXTURES/'run_proof.mjs'), str(FIXTURES/'silent_handler.mjs')],
                             capture_output=True, text=True, timeout=30)
        assert json.loads(out.stdout) == {'ok': False, 'error': 'SilenceBreach'}


def test_report_mode_logs_the_breach_and_carries_on(caplog):
    module = load(FIXTURES/'silent_handler.py')
    from livestack_node.workloads.supervised.simulate import FakeClock, SimBackend
    emitted, fake = [], FakeClock()
    with caplog.at_level('WARNING'):
        module.handler.rebuild(mode='report', clock=fake, backend=SimBackend(fake, module.SCENARIO),
                               sink=emitted.append)()
    assert 'silent for more than 60s' in caplog.text and emitted


def test_real_backend_ticks_per_output_chunk_and_enforce_kills_a_silent_child(tmp_path, monkeypatch):
    monkeypatch.setenv('HARMONY_OUTPUT', str(tmp_path))
    seen = []

    @sv.supervised({'max_silence_seconds': 30}, sink=seen.append)
    def talkative(ctx):
        return ctx.exec(sys.executable, ['-c', 'import sys,time\nfor n in range(3):\n print(n, flush=True); time.sleep(.3)'])
    result = talkative()
    assert result['code'] == 0 and result['tail'].split() == [b'0', b'1', b'2']
    assert seen and seen[0]['phase'] == 'start' and seen[0]['beat'] >= 1

    # Enforce: a child that prints nothing is killed when the silence limit passes. The limit is
    # tightened below the contract floor through the reducer state, since the contract floor is 30 s.
    @sv.supervised({'max_silence_seconds': 30}, mode='enforce', sink=lambda doc: None)
    def silent(ctx):
        ctx.state['contract']['max_silence_seconds'] = 0.5
        return ctx.exec(sys.executable, ['-c', 'import time; time.sleep(60)'])
    with pytest.raises(SilenceBreach):
        silent()


def test_write_progress_accepts_beat_and_refuses_junk(tmp_path, monkeypatch):
    monkeypatch.setenv('HARMONY_OUTPUT', str(tmp_path))
    lease_helper.write_progress({'phase': 'x', 'beat': 3})
    assert json.loads((tmp_path/'progress.json').read_text()) == {'phase': 'x', 'beat': 3}
    with pytest.raises(WorkloadError):
        lease_helper.write_progress({'phase': 'x', 'beat': -1})
