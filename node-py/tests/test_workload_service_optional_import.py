"""The source-only workload runtime can start without the optional GitHub extra."""
import os
from pathlib import Path
import subprocess
import sys


def test_workload_service_imports_without_unconfigured_github_crypto(tmp_path):
    node_py = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc
import sys

class BlockCryptography(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ModuleNotFoundError('blocked optional dependency', name=fullname)

sys.meta_path.insert(0, BlockCryptography())
import livestack_node.workloads.service
"""
    environment = dict(os.environ, PYTHONPATH=str(node_py))
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
