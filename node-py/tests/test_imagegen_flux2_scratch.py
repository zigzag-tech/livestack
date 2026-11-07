"""The offload scratch dir is configuration, never a worker workspace (no GPU needed)."""
from livestack_node.imagegen.flux2 import offload_scratch_dir


def test_default_is_system_temp(monkeypatch):
    monkeypatch.delenv("LIVESTACK_OFFLOAD_DIR", raising=False)
    assert offload_scratch_dir() is None


def test_env_names_and_creates_the_dir(monkeypatch, tmp_path):
    target = tmp_path / "a" / "offload"
    monkeypatch.setenv("LIVESTACK_OFFLOAD_DIR", str(target))
    assert offload_scratch_dir() == str(target) and target.is_dir()


def test_no_source_hardwires_a_workload_workspace():
    import pathlib, livestack_node.imagegen.flux2 as m
    assert "livestack-workloads/" not in pathlib.Path(m.__file__).read_text()
