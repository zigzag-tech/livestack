import pytest

from livestack_node.policy_lab.datasets import (
    DatasetError,
    DatasetManifest,
    Episode,
    ExposureLedger,
)


def _episodes():
    return (
        Episode("e1", "session-a", "workflow-a", "window-1", "hash-a"),
        Episode("e2", "session-a", "workflow-a", "window-1", "hash-b"),
        Episode("e3", "session-b", "workflow-b", "window-2", "hash-c"),
    )


def test_s27_whole_workflow_cannot_be_split_across_partitions():
    with pytest.raises(DatasetError, match="whole episode group"):
        DatasetManifest.create(
            _episodes(),
            assignments={"e1": "training", "e2": "holdout", "e3": "calibration"},
        )


def test_manifest_hash_detects_tampering_and_near_duplicate_cross_partition():
    manifest = DatasetManifest.create(
        _episodes(),
        assignments={"e1": "training", "e2": "training", "e3": "holdout"},
    )
    manifest.verify()
    object.__setattr__(manifest, "assignments", (("e1", "holdout"),))
    with pytest.raises(DatasetError, match="hash mismatch"):
        manifest.verify()

    duplicates = (
        Episode("a", "s1", "w1", "t1", "same-near-duplicate"),
        Episode("b", "s2", "w2", "t2", "same-near-duplicate"),
    )
    with pytest.raises(DatasetError, match="near-duplicate"):
        DatasetManifest.create(
            duplicates, assignments={"a": "training", "b": "holdout"}
        )


def test_holdout_exposure_is_recorded_and_hidden_from_author_view():
    manifest = DatasetManifest.create(
        _episodes(),
        assignments={"e1": "regression", "e2": "regression", "e3": "holdout"},
    )
    assert {episode.episode_id for episode in manifest.author_visible()} == {"e1", "e2"}
    ledger = ExposureLedger()
    ledger.record("candidate-a", manifest, partition="holdout", evaluator_identity="grader")
    assert ledger.count("candidate-a", manifest.manifest_sha256, "holdout") == 1
    with pytest.raises(DatasetError, match="evaluator identity"):
        ledger.record("candidate-a", manifest, partition="holdout", evaluator_identity="author")
