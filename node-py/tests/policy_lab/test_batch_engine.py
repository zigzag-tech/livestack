import pytest

from livestack_node.policy_lab.batch_engine import BatchError, BatchStage


def test_batch_interrupts_only_at_checkpoint_and_publishes_declared_artifacts():
    stage = BatchStage(
        "render",
        total_work_units=100,
        checkpoints=(25, 50, 75),
        resource_vector={"cpu_millicores": 1000},
        output_artifacts=("video-digest",),
        eligible_at_us=10,
    )
    stage.start(at_us=20)
    stage.advance_to(40)
    with pytest.raises(BatchError, match="checkpoint"):
        stage.interrupt()
    stage.advance_to(50)
    stage.interrupt()
    stage.resume()
    stage.advance_to(100)
    assert stage.complete(at_us=200) == ("video-digest",)
    assert stage.wait_for_eligibility_us == 10


def test_semantic_failure_is_not_retried_by_scheduler():
    stage = BatchStage(
        "repairable",
        total_work_units=10,
        checkpoints=(),
        resource_vector={"gpu_bytes": 1},
        output_artifacts=(),
        eligible_at_us=0,
    )
    stage.start(at_us=0)
    stage.fail("semantic_validation")
    with pytest.raises(BatchError, match="semantic repair"):
        stage.scheduler_retry()


def test_starvation_age_counts_continuous_eligibility():
    stage = BatchStage(
        "background",
        total_work_units=1,
        checkpoints=(),
        resource_vector={"cpu_millicores": 1},
        output_artifacts=(),
        eligible_at_us=100,
    )
    assert stage.starvation_age_us(at_us=350) == 250
