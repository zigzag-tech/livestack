"""Subprocess-isolated deterministic policy calls with finite budgets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import ContractError


RUNNER = r'''
import json
import resource
import signal
import sys

def cpu_expired(signum, frame):
    raise TimeoutError("cpu budget exceeded")

request = json.loads(sys.stdin.read())
resource.setrlimit(resource.RLIMIT_AS, (request["memory_bytes"], request["memory_bytes"]))
signal.signal(signal.SIGPROF, cpu_expired)
signal.setitimer(signal.ITIMER_PROF, request["cpu_ms"] / 1000.0)
safe_builtins = {
    "bool": bool, "dict": dict, "enumerate": enumerate, "float": float,
    "int": int, "len": len, "list": list, "max": max, "min": min,
    "range": range, "reversed": reversed, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip,
}
namespace = {"__builtins__": safe_builtins}
try:
    exec(compile(request["source"], "<candidate-policy>", "exec"), namespace, namespace)
    decide = namespace.get("decide")
    if not callable(decide):
        raise ValueError("candidate must define decide(observation)")
    output = decide(request["observation"])
    encoded = json.dumps(output, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > request["output_bytes"]:
        print(json.dumps({"status": "output_budget"}))
    else:
        print(json.dumps({"status": "ok", "output": output}, allow_nan=False, separators=(",", ":"), sort_keys=True))
except TimeoutError:
    print(json.dumps({"status": "runtime_budget"}))
except MemoryError:
    print(json.dumps({"status": "memory_budget"}))
except BaseException as exc:
    print(json.dumps({"status": "invalid_policy", "error_type": type(exc).__name__}))
'''


@dataclass(frozen=True)
class PolicyRunResult:
    status: str
    output: Any = None
    error_type: str | None = None


class PolicySandbox:
    def __init__(self, *, cpu_ms: int = 50, memory_bytes: int = 256 * 1024 * 1024, output_bytes: int = 1024 * 1024) -> None:
        if min(cpu_ms, memory_bytes, output_bytes) <= 0:
            raise ContractError("policy sandbox budgets must be positive")
        self.cpu_ms = cpu_ms
        self.memory_bytes = memory_bytes
        self.output_bytes = output_bytes

    def run(self, source: str, observation: Mapping[str, Any]) -> PolicyRunResult:
        request = json.dumps(
            {
                "source": source,
                "observation": observation,
                "cpu_ms": self.cpu_ms,
                "memory_bytes": self.memory_bytes,
                "output_bytes": self.output_bytes,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with tempfile.TemporaryDirectory(prefix="harmony-policy-") as directory:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", RUNNER],
                    input=request,
                    text=True,
                    capture_output=True,
                    cwd=directory,
                    env={"PATH": os.environ.get("PATH", "")},
                    timeout=max(1.0, self.cpu_ms / 1000.0 * 10),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return PolicyRunResult("runtime_budget")
        if completed.returncode < 0:
            if completed.returncode in {-9, -24, -27}:
                return PolicyRunResult("runtime_budget")
            return PolicyRunResult("invalid_policy", error_type=f"signal:{-completed.returncode}")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return PolicyRunResult("invalid_policy", error_type="invalid_runner_output")
        return PolicyRunResult(
            response.get("status", "invalid_policy"),
            response.get("output"),
            response.get("error_type"),
        )
