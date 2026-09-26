"""Tests for the MESHLINK.lock drift check (openspec task 7.2, DR-5).

The check is scripts/check-meshlink-pin.mjs; these tests drive the REAL
script end-to-end (subprocess, exit codes) rather than importing its pure
helper — the instrument under test is the CI entry point, and a test of a
copy would only prove the copy. The positive control (real lock, real
sibling) must pass here; the fabricated-drift cases must exit non-zero and
name the mismatch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "scripts" / "check-meshlink-pin.mjs"
LOCK = REPO_ROOT / "MESHLINK.lock"
MESHLINK_REPO = Path(os.environ.get("MESHLINK_REPO") or REPO_ROOT.parent / "meshlink")

PIN_LINE = next(line for line in LOCK.read_text().splitlines()
                if line.startswith("MESHLINK_REV="))
PINNED_REV = PIN_LINE.split("=", 1)[1].strip()


def _run(*extra: str) -> subprocess.CompletedProcess:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    return subprocess.run(["node", str(CHECK), *extra],
                          capture_output=True, text=True, timeout=30)


def test_lock_pins_a_full_sha_and_names_what_it_covers():
    text = LOCK.read_text()
    assert PINNED_REV and len(PINNED_REV) == 40, PIN_LINE
    assert "mesh_relay" in text and "crate" in text.lower(), \
        "lock header must state the pin covers the crate AND the relay (DR-5)"
    assert "benchday" in text.lower(), \
        "lock header must state benchday owns its own pin (DR-5)"


@pytest.mark.skipif(not (MESHLINK_REPO / ".git").exists(),
                    reason="meshlink sibling checkout not present")
def test_positive_control_real_lock_matches_sibling_head():
    proc = _run("--lock", str(LOCK), "--sibling", str(MESHLINK_REPO))
    assert proc.returncode == 0, proc.stderr
    assert PINNED_REV in proc.stdout


@pytest.mark.skipif(not (MESHLINK_REPO / ".git").exists(),
                    reason="meshlink sibling checkout not present")
def test_fabricated_drift_exits_nonzero(tmp_path):
    drifted = tmp_path / "MESHLINK.lock"
    drifted.write_text(LOCK.read_text().replace(
        PIN_LINE, "MESHLINK_REV=" + "0" * 40))
    proc = _run("--lock", str(drifted), "--sibling", str(MESHLINK_REPO))
    assert proc.returncode != 0
    assert "0" * 40 in proc.stderr and "mismatch" in proc.stderr


def test_missing_lock_fails_closed(tmp_path):
    proc = _run("--lock", str(tmp_path / "absent.lock"),
                "--sibling", str(tmp_path / "absent-dir"))
    assert proc.returncode != 0
    assert "missing" in proc.stderr


def test_lock_without_rev_line_fails_closed(tmp_path):
    lock = tmp_path / "MESHLINK.lock"
    lock.write_text("# header only, no pin\n")
    proc = _run("--lock", str(lock), "--sibling", str(MESHLINK_REPO))
    assert proc.returncode != 0
    assert "MESHLINK_REV" in proc.stderr
