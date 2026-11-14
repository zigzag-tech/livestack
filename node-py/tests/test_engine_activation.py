"""Real git/filesystem activation tests. Inject only the lease/restart boundary
to exercise refusal and rollback without touching a live broker or service.
"""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from livestack_node.engine_activation import apply, digest, encoded, plan


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'
    (source / 'serve').mkdir(parents=True)
    (source / 'serve/server.py').write_text('print("candidate")\n')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Activation Test',
        '-c', 'user.email=activation@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    rev = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    manifest = {'source': rev, 'files': {}}
    for name in ('strata', 'libstrata_kq.so'):
        raw = ('verified-' + name).encode()
        (artifacts / name).write_bytes(raw)
        if name == 'strata':
            (artifacts / name).chmod(0o755)
        manifest['files'][name] = {'bytes': len(raw), 'sha256': digest(raw)}
    (artifacts / 'receipt.json').write_bytes(encoded(manifest))
    config = tmp_path / 'old-runtime.json'
    config.write_bytes(encoded({'exe': '/old/strata', 'args': ['--gpu', '1'],
        'model_name': 'qwen3.8-flash-next-q2_0', 'lib_dirs': ['/old/libs']}))
    unit = {'name': 'flash_next', 'engine': 'strata', 'engine_source': {'rev': 'old'},
        'strata_root': '/old/source', 'strata_config': str(config),
        'attributes': {'params_b': 125}, 'exclusive_device': True}
    units = tmp_path / 'units.json'
    units.write_bytes(encoded([unit, {'name': 'unrelated', 'attributes': {'keep': True}}]))
    operations = tmp_path / 'operations'
    operations.mkdir(mode=0o700)
    args = SimpleNamespace(source_rev=rev, source_root=str(source), python=sys.executable,
        artifacts=str(artifacts / 'receipt.json'), units_file=str(units),
        expected_units_sha256=digest(units.read_bytes()), expected_source_rev='old',
        unit='flash_next', operation_dir=str(operations / 'one'), service='harmony-llm.service')
    return args


class Lease:
    lease_id = 'test-exclusive-lease'
    def __init__(self, args):
        self.args = args
    def __enter__(self):
        return self
    def check(self):
        pass
    def __exit__(self, *exc):
        pass


def test_plan_is_read_only_and_preserves_unrelated_units(setup):
    before = Path(setup.units_file).read_bytes()
    value = plan(setup)
    assert Path(setup.units_file).read_bytes() == before
    assert not (Path(setup.source_root) / 'STRATA_VERSION').exists()
    assert json.loads(value['candidate'])[1] == json.loads(before)[1]
    selected = json.loads(value['candidate'])[0]
    assert selected['engine_source']['rev'] == setup.source_rev
    assert selected['attributes'] == json.loads(before)[0]['attributes']


@pytest.mark.parametrize('fault', ['artifact', 'source', 'pin', 'declaration', 'untracked', 'service'])
def test_preflight_refuses_without_admission_or_selection(setup, fault):
    if fault == 'artifact':
        (Path(setup.artifacts).parent / 'strata').write_bytes(b'tampered')
    elif fault == 'source':
        (Path(setup.source_root) / 'serve/server.py').write_text('dirty\n')
    elif fault == 'pin':
        setup.expected_source_rev = 'another'
    elif fault == 'declaration':
        Path(setup.units_file).write_bytes(b'[]')
    elif fault == 'untracked':
        Path(setup.source_root, 'serve/shadow.py').write_text('untracked\n')
    else:
        setup.service = 'unrelated.service'
    before = Path(setup.units_file).read_bytes()
    def forbidden(_):
        pytest.fail('invalid candidate reached admission')
    with pytest.raises(ValueError):
        apply(setup, lease_factory=forbidden)
    assert Path(setup.units_file).read_bytes() == before


def test_apply_selects_under_lease_and_records_completion(setup):
    calls = []
    class TrackedLease(Lease):
        def __enter__(self):
            calls.append('admitted')
            return self
        def check(self):
            calls.append('lease-checked')
        def __exit__(self, *exc):
            calls.append('released')
    def restart(service):
        assert 'admitted' in calls and 'released' not in calls
        assert json.loads(Path(setup.units_file).read_bytes())[0]['engine_source']['rev'] == setup.source_rev
        calls.append(service)
    result = apply(setup, lease_factory=TrackedLease, restart_service=restart)
    assert result['phase'] == 'completed' and calls[-1] == 'released'
    assert Path(setup.source_root, 'STRATA_VERSION').read_text().strip() == setup.source_rev
    journal = json.loads(Path(setup.operation_dir, 'journal.json').read_bytes())
    assert journal['lease_id'] == Lease.lease_id and journal['phase'] == 'completed'
    assert Path(setup.operation_dir, 'previous-units.json').stat().st_mode & 0o077 == 0


def test_private_runtime_is_written_for_the_source_service_owner(setup, monkeypatch):
    import livestack_node.engine_activation as activation
    real_atomic = activation.atomic
    ownership = []
    def capture(path, raw, *args, **kwargs):
        if Path(path).name == 'operator-runtime.json':
            ownership.append((kwargs.get('uid'), kwargs.get('gid')))
        return real_atomic(path, raw, *args, **kwargs)
    monkeypatch.setattr(activation, 'atomic', capture)
    apply(setup, lease_factory=Lease, restart_service=lambda _: None)
    owner = Path(setup.source_root).stat()
    assert ownership == [(owner.st_uid, owner.st_gid)]
    assert Path(setup.source_root, 'operator-runtime.json').stat().st_mode & 0o077 == 0


def test_failed_restart_restores_exact_original_and_keeps_failure(setup):
    original = Path(setup.units_file).read_bytes()
    calls = []
    def restart(_):
        calls.append(Path(setup.units_file).read_bytes())
        if len(calls) == 1:
            raise RuntimeError('candidate failed')
    with pytest.raises(RuntimeError, match='previous declaration restored'):
        apply(setup, lease_factory=Lease, restart_service=restart)
    assert len(calls) == 2 and calls[1] == original
    assert Path(setup.units_file).read_bytes() == original
    assert json.loads(Path(setup.operation_dir, 'journal.json').read_bytes())['phase'] == 'rolled_back'


def test_admission_refusal_leaves_live_files_unchanged(setup):
    original = Path(setup.units_file).read_bytes()
    class Refused(Lease):
        def __enter__(self):
            raise RuntimeError('busy speech reservation')
    with pytest.raises(RuntimeError, match='busy speech'):
        apply(setup, lease_factory=Refused, restart_service=lambda _: pytest.fail('restarted'))
    assert Path(setup.units_file).read_bytes() == original
    assert not Path(setup.source_root, 'operator-runtime.json').exists()


def test_edit_during_admission_is_not_overwritten(setup):
    changed = encoded([{'name': 'another-owner'}])
    class Changed(Lease):
        def __enter__(self):
            Path(self.args.units_file).write_bytes(changed)
            return self
    with pytest.raises(ValueError, match='digest changed'):
        apply(setup, lease_factory=Changed, restart_service=lambda _: pytest.fail('restarted'))
    assert Path(setup.units_file).read_bytes() == changed


def test_rollback_refuses_to_overwrite_another_deployment(setup):
    changed = encoded([{'name': 'newer-deployment'}])
    def restart(_):
        Path(setup.units_file).write_bytes(changed)
        raise RuntimeError('candidate failed after concurrent edit')
    with pytest.raises(RuntimeError, match='another declaration owns'):
        apply(setup, lease_factory=Lease, restart_service=restart)
    assert Path(setup.units_file).read_bytes() == changed


def test_lease_release_failure_is_not_reported_as_success(setup):
    class ReleaseLost(Lease):
        def __exit__(self, *exc):
            raise RuntimeError('release unconfirmed')
    with pytest.raises(RuntimeError, match='release unconfirmed'):
        apply(setup, lease_factory=ReleaseLost, restart_service=lambda _: None)
    assert json.loads(Path(setup.operation_dir, 'journal.json').read_bytes())['phase'] == 'failed_after_active'


def test_receipt_count_is_bounded(setup):
    parent = Path(setup.operation_dir).parent
    for i in range(8):
        (parent / str(i)).mkdir()
    with pytest.raises(ValueError, match='count bound'):
        apply(setup, lease_factory=lambda _: pytest.fail('admitted'))


def test_operator_lease_uses_real_host_broker_admission_and_release(tmp_path, monkeypatch):
    """TCP/ASGI positive control: invented grants cannot pass as real exclusivity."""
    import socket
    import threading
    import time
    import uvicorn
    from test_exclusive_http import _Peer, _broker
    from livestack_node.hostd import build_app
    from livestack_node.engine_activation import DeviceLease

    monkeypatch.setenv('LIVESTACK_LEDGER_DIR', str(tmp_path / 'ledger'))
    peer = _Peer('http://isolated-peer', 'flash_next')
    broker = _broker([peer], time.monotonic)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(broker), log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    token = tmp_path / 'token'
    token.write_text('isolated-test-credential')
    token.chmod(0o600)
    args = SimpleNamespace(broker_url=f'http://127.0.0.1:{port}', token_file=str(token),
        device_id='gpu0', unit='flash_next', owner='isolated-operator', wait_seconds=2,
        min_free_bytes=24000)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with DeviceLease(args) as lease:
            lease.check()
            assert broker.exclusive_store.get_lease(lease.lease_id)['state'] == 'exclusive'
            assert ('evict', 'flash_next') in peer.calls
        assert broker.exclusive_store.get_lease(lease.lease_id)['state'] == 'released'
        args.min_free_bytes = 0
        with pytest.raises(RuntimeError, match='broker request refused: HTTP 400'):
            with DeviceLease(args):
                pytest.fail('invalid broker request acquired a lease')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        broker.exclusive_store.close()
    assert not thread.is_alive()


def test_sudo_reads_original_callers_private_credential(tmp_path, monkeypatch):
    import livestack_node.engine_activation as activation
    token = tmp_path / 'credential'
    token.write_bytes(b'private-value')
    token.chmod(0o600)
    monkeypatch.setenv('SUDO_UID', str(token.stat().st_uid))
    monkeypatch.setattr(activation.os, 'getuid', lambda: 0)
    assert activation.read(token, 256, private=True) == b'private-value'
    token.chmod(0o640)
    with pytest.raises(ValueError, match='owned private'):
        activation.read(token, 256, private=True)


def test_cli_defaults_to_positive_exclusive_memory(setup, monkeypatch):
    import livestack_node.engine_activation as activation
    argv = ['activate']
    for key in ('units_file', 'unit', 'expected_source_rev', 'expected_units_sha256',
                'source_root', 'source_rev', 'artifacts', 'python'):
        argv += ['--' + key.replace('_', '-'), getattr(setup, key)]
    captured = []
    original = activation.plan
    def capture(args):
        captured.append(args.min_free_bytes)
        return original(args)
    monkeypatch.setattr(activation, 'plan', capture)
    monkeypatch.setattr(sys, 'argv', argv)
    assert activation.main() == 0
    assert captured == [1]
    monkeypatch.setattr(sys, 'argv', argv + ['--min-free-bytes', '0'])
    with pytest.raises(SystemExit) as error:
        activation.main()
    assert error.value.code == 2
    assert captured == [1]
