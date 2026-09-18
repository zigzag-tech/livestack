import hashlib
import json

import pytest

from livestack_node.policy_lab.candidate_package import validate_candidate_package
from livestack_node.policy_lab.contracts import ContractError


def _write_package(root):
    root.mkdir()
    policy = b"def decide(observation):\n    return {'chosen': None}\n"
    (root / "policy.py").write_bytes(policy)
    (root / "hypothesis.json").write_text(
        json.dumps({"hypothesis": "reduce queueing", "expected_tradeoff": "more transfer"}),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "kind": "candidate_package",
        "candidate_id": "candidate-a",
        "files": {
            "policy.py": hashlib.sha256(policy).hexdigest(),
            "hypothesis.json": hashlib.sha256((root / "hypothesis.json").read_bytes()).hexdigest(),
        },
        "profile_sha256": "a" * 64,
        "evaluator_sha256": "b" * 64,
        "dataset_sha256": "c" * 64,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_candidate_package_accepts_only_declared_policy_and_hypothesis(tmp_path):
    root = tmp_path / "candidate"
    manifest = _write_package(root)
    report = validate_candidate_package(
        root,
        expected_profile_sha256=manifest["profile_sha256"],
        expected_evaluator_sha256=manifest["evaluator_sha256"],
        expected_dataset_sha256=manifest["dataset_sha256"],
    )
    assert report["status"] == "valid"
    assert report["network_allowed"] is False
    assert report["immutable_evaluator_inputs"] is True


@pytest.mark.parametrize("attack", ["extra", "symlink", "mutated_evaluator", "network_import"])
def test_candidate_package_rejects_escape_mutation_network_and_extra_files(tmp_path, attack):
    root = tmp_path / "candidate"
    manifest = _write_package(root)
    if attack == "extra":
        (root / "extra.py").write_text("pass")
    elif attack == "symlink":
        (root / "escape").symlink_to("/etc/passwd")
    elif attack == "mutated_evaluator":
        manifest["evaluator_sha256"] = "d" * 64
        (root / "manifest.json").write_text(json.dumps(manifest))
    else:
        (root / "policy.py").write_text("import socket\ndef decide(observation): return None\n")
        manifest["files"]["policy.py"] = hashlib.sha256((root / "policy.py").read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError):
        validate_candidate_package(
            root,
            expected_profile_sha256="a" * 64,
            expected_evaluator_sha256="b" * 64,
            expected_dataset_sha256="c" * 64,
        )
