from livestack_node.workloads.supervision import SystemdExecutor
from livestack_node.workloads.worker import WorkloadWorker


def test_worker_passes_identity_requirement_only_for_linux_compilation():
    worker = object.__new__(WorkloadWorker)
    worker.executor = SystemdExecutor('identity-routing-test')

    compilation = {'version': 1, 'classes': ['rust', 'native']}
    assert worker._executor_identity_options({'compilation': compilation}) == {
        'host_identity_required': True}
    assert worker._executor_identity_options({'compilation': None}) == {
        'host_identity_required': False}


def test_worker_leaves_platform_specific_verifiers_unchanged():
    worker = object.__new__(WorkloadWorker)
    worker.executor = object()

    assert worker._executor_identity_options({'compilation': {'version': 1}}) == {}
    assert worker._executor_identity_options({}) == {}
