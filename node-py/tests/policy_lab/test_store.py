import pytest

from livestack_node.policy_lab.store import ArtifactStore, StoreFullError


def test_full_store_refuses_when_no_retention_window_is_configured(tmp_path):
    store = ArtifactStore(tmp_path / "lab", max_bytes=5, retention_window_us=None)
    store.put(b"12345", created_at_us=0)
    with pytest.raises(StoreFullError):
        store.put(b"x", created_at_us=100)
    assert store.used_bytes == 5


def test_retention_never_deletes_active_release_pinned_or_referenced(tmp_path):
    store = ArtifactStore(tmp_path / "lab", max_bytes=20, retention_window_us=10)
    pinned = store.put(b"pin00", created_at_us=0, pinned=True)
    release = store.put(b"rel00", created_at_us=0, release_evidence=True)
    active = store.put(b"act00", created_at_us=0, active=True)
    referenced = store.put(b"ref00", created_at_us=0)
    store.add_reference(referenced)

    with pytest.raises(StoreFullError):
        store.put(b"new00", created_at_us=100)
    assert {pinned, release, active, referenced}.issubset(store.artifact_ids())


def test_only_old_unprotected_lab_artifact_is_pruned(tmp_path):
    store = ArtifactStore(tmp_path / "lab", max_bytes=10, retention_window_us=10)
    old = store.put(b"old00", created_at_us=0)
    keep = store.put(b"keep0", created_at_us=95)
    new = store.put(b"new00", created_at_us=100)
    assert old not in store.artifact_ids()
    assert {keep, new} == store.artifact_ids()
    assert store.used_bytes == 10


def test_release_reference_cannot_be_removed_as_an_ordinary_reference(tmp_path):
    store = ArtifactStore(tmp_path / "lab", max_bytes=10, retention_window_us=1)
    artifact_id = store.put(b"proof", created_at_us=0, release_evidence=True)
    with pytest.raises(ValueError, match="release evidence"):
        store.remove_reference(artifact_id)
