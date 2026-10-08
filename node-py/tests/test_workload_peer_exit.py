"""A peer that exits mid-verification is a named, retryable refusal, never admission."""
import pytest

from livestack_node.workloads import launch_guard, launch_verifier
from livestack_node.workloads.launch_contract import PEER_EXITED
from livestack_node.workloads.model import WorkloadError


def fake_proc(tmp_path, pid, cgroup='0::/work.service\n'):
    base = tmp_path/str(pid)
    base.mkdir()
    (base/'stat').write_text('1 (a b) S 0 ' + ' '.join(['0']*40))
    (base/'cgroup').write_text(cgroup)


def test_positive_control_reads_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_verifier, 'PROC', tmp_path)
    fake_proc(tmp_path, 7)
    start, group = launch_verifier.process_identity(7)
    assert group == '/work.service' and start == '0'


@pytest.mark.parametrize('missing', ['stat', 'cgroup', 'both'])
def test_exited_peer_is_named_and_retryable(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(launch_verifier, 'PROC', tmp_path)
    if missing != 'both':
        fake_proc(tmp_path, 7)
        (tmp_path/'7'/missing).unlink()
    with pytest.raises(WorkloadError) as caught:
        launch_verifier.process_identity(7)
    assert str(caught.value) == PEER_EXITED and caught.value.status == 503


@pytest.mark.parametrize('cgroup', ['0::relative\n', '1:cpu:/x\n'])
def test_real_identity_failures_still_refuse_as_before(tmp_path, monkeypatch, cgroup):
    monkeypatch.setattr(launch_verifier, 'PROC', tmp_path)
    fake_proc(tmp_path, 7, cgroup=cgroup)
    with pytest.raises(WorkloadError) as caught:
        launch_verifier.process_identity(7)
    assert str(caught.value) == 'compilation_peer_cgroup_unknown'


def guard_env(monkeypatch, tmp_path, outcomes):
    monkeypatch.setenv('HARMONY_OUTPUT', str(tmp_path))
    calls = []
    def fake_verify(request, registry_path=None):
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes))-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(launch_guard, 'environment_request', lambda c: dict.fromkeys(('worker', 'host', 'policy_revision', 'job_id', 'attempt_id', 'input_digest'), 'x'))
    monkeypatch.setattr(launch_guard, 'verify_launch', fake_verify)
    monkeypatch.setattr(launch_guard, 'write_current_receipt', lambda *a: None)
    return calls


RECEIPT = dict(classes=['rust'], worker='w', attempt_id='a')


def test_guard_retries_peer_exit_once_then_admits(tmp_path, monkeypatch, caplog):
    calls = guard_env(monkeypatch, tmp_path, [WorkloadError(PEER_EXITED, 503), RECEIPT])
    caplog.set_level('INFO')
    assert launch_guard.require_compilation('rust') == RECEIPT
    assert len(calls) == 2 and 'compilation_launch_retry' in caplog.text


def test_guard_peer_exit_twice_refuses_without_third_try(tmp_path, monkeypatch):
    calls = guard_env(monkeypatch, tmp_path, [WorkloadError(PEER_EXITED, 503)])
    with pytest.raises(WorkloadError, match=PEER_EXITED):
        launch_guard.require_compilation('rust')
    assert len(calls) == 2


@pytest.mark.parametrize('reason', ['compilation_peer_outside_attempt', 'compilation_verification_unavailable',
                                    'compilation_peer_containment_changed'])
def test_guard_never_retries_real_refusals(tmp_path, monkeypatch, reason):
    calls = guard_env(monkeypatch, tmp_path, [WorkloadError(reason, 403), RECEIPT])
    with pytest.raises(WorkloadError, match=reason):
        launch_guard.require_compilation('rust')
    assert len(calls) == 1
