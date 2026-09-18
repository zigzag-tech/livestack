"""Bounded periodic policy-cycle planning atop the durable workload authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import ContractError
from .policy_sandbox import PolicySandbox


PIPELINE = ("planned", "admitted", "gathering", "frozen", "authoring", "evaluating", "reported")
TERMINAL = frozenset({"no_change", "insufficient_evidence", "failed", "canceled", "offline_qualified"})


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{field} must be a SHA-256 digest")
    return value


def _bounded_int(value: Any, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f"{field} must be in [{low}, {high}]")
    return value


def plan_cycle(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("schema_version") != 1 or config.get("kind") != "cycle_config":
        raise ContractError("cycle config must be a schema_version 1 cycle_config")
    domain = config.get("domain_id")
    cutoff = config.get("data_cutoff_utc_us")
    if not isinstance(domain, str) or not domain or type(cutoff) is not int or cutoff < 0:
        raise ContractError("cycle domain and nonnegative data cutoff are required")
    hashes = {
        field: _digest(config.get(field), field)
        for field in ("source_sha256", "profile_sha256", "dataset_sha256", "evaluator_sha256")
    }
    limits = {
        "candidate_limit": _bounded_int(config.get("candidate_limit"), "candidate_limit", 1, 4),
        "revisions_per_candidate": _bounded_int(config.get("revisions_per_candidate"), "revisions_per_candidate", 0, 2),
        "evaluation_limit": _bounded_int(config.get("evaluation_limit"), "evaluation_limit", 1, 8),
        "wall_seconds": _bounded_int(config.get("wall_seconds"), "wall_seconds", 1, 7200),
        "cpu": _bounded_int(config.get("cpu"), "cpu", 1, 4),
        "memory_bytes": _bounded_int(config.get("memory_bytes"), "memory_bytes", 1, 8 * 1024**3),
        "output_bytes": _bounded_int(config.get("output_bytes"), "output_bytes", 1, 1024**3),
        "gpu_seconds": _bounded_int(config.get("gpu_seconds"), "gpu_seconds", 0, 0),
    }
    token_budget = config.get("authoring_token_budget")
    spend_budget = config.get("authoring_spend_microusd")
    authoring_enabled = (
        type(token_budget) is int and token_budget > 0
        and type(spend_budget) is int and spend_budget > 0
    )
    if (token_budget is None) != (spend_budget is None):
        raise ContractError("authoring token and spend budgets must both be set or both absent")
    weekly = config.get("weekly_trigger_enabled")
    if type(weekly) is not bool:
        raise ContractError("weekly_trigger_enabled must be boolean")
    canonical = json.dumps(config, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    config_hash = hashlib.sha256(canonical).hexdigest()
    cycle_id = hashlib.sha256(f"{domain}\0{cutoff}\0{config_hash}".encode()).hexdigest()[:32]
    return {
        "schema_version": 1,
        "kind": "cycle_manifest",
        "cycle_id": cycle_id,
        "idempotency_key": f"policy-lab-cycle/{domain}/{cutoff}",
        "domain_id": domain,
        "data_cutoff_utc_us": cutoff,
        "config_sha256": config_hash,
        **hashes,
        **limits,
        "authoring_token_budget": token_budget,
        "authoring_spend_microusd": spend_budget,
        "mode": "author_and_evaluate" if authoring_enabled else "report_only",
        "state": "planned",
        "submission_count": 0,
        "scheduler_installed": False,
        "weekly_trigger_requested": weekly,
        "live_activation_allowed": False,
    }


def submit_cycle(plan: Any, authority: Any, *, handler: str, input_digest: str) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("kind") != "cycle_manifest":
        raise ContractError("cycle submission requires a cycle manifest")
    request = {
        "version": 1,
        "key": plan["idempotency_key"],
        "handler": handler,
        "input_digest": input_digest,
        "payload": {"cycle_manifest": plan},
        "need": {"cpu": plan["cpu"], "memory_bytes": plan["memory_bytes"]},
        "estimate_seconds": plan["wall_seconds"],
        "retain": True,
    }
    result = authority.submit(request)
    job_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise ContractError("workload authority returned no cycle job id")
    return {"cycle_id": plan["cycle_id"], "job_id": job_id, "state": result.get("state", "admitted")}


def coalesce_trigger(active: Any, incoming: Any) -> dict[str, Any]:
    if active.get("domain_id") != incoming.get("domain_id"):
        raise ContractError("cannot coalesce different policy domains")
    return {
        "active_cycle_id": active["cycle_id"],
        "next_data_cutoff_utc_us": max(active["data_cutoff_utc_us"], incoming["data_cutoff_utc_us"]),
        "coalesced": incoming["cycle_id"] != active["cycle_id"],
    }


@dataclass
class CycleMachine:
    cycle_id: str
    candidate_limit: int
    revision_limit: int
    evaluation_limit: int
    state: str = "planned"
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    evaluations: int = 0
    hidden_evaluation_exposures: int = 0
    incumbent_changed: bool = False

    def advance(self, target: str) -> str:
        if self.state in TERMINAL:
            raise ContractError("cycle is terminal")
        if target in TERMINAL:
            if self.state != "reported":
                raise ContractError("terminal cycle outcome requires reported state")
            self.state = target
            return target
        try:
            current = PIPELINE.index(self.state)
            wanted = PIPELINE.index(target)
        except ValueError as exc:
            raise ContractError("unknown cycle state") from exc
        if wanted != current + 1:
            raise ContractError("cycle transitions must advance exactly one durable stage")
        self.state = target
        return target

    def record_candidate(self, candidate_id: str, hypothesis: str, tradeoff: str) -> None:
        if self.state != "authoring" or len(self.candidates) >= self.candidate_limit:
            raise ContractError("candidate authoring is unavailable or exhausted")
        if candidate_id in self.candidates or not all(isinstance(v, str) and v for v in (candidate_id, hypothesis, tradeoff)):
            raise ContractError("candidate identity/hypothesis/tradeoff is invalid")
        self.candidates[candidate_id] = {"hypothesis": hypothesis, "tradeoff": tradeoff, "revisions": 0}

    def record_revision(self, candidate_id: str) -> None:
        candidate = self.candidates.get(candidate_id)
        if candidate is None or candidate["revisions"] >= self.revision_limit:
            raise ContractError("candidate revision budget exhausted")
        candidate["revisions"] += 1

    def record_evaluation(self, candidate_id: str, *, status: str, holdout_exposure: bool = True) -> None:
        if self.state != "evaluating" or candidate_id not in self.candidates or self.evaluations >= self.evaluation_limit:
            raise ContractError("evaluation is unavailable or exhausted")
        if status not in {"rejected", "no_change", "offline_qualified"}:
            raise ContractError("invalid candidate evaluation status")
        self.evaluations += 1
        if holdout_exposure:
            self.hidden_evaluation_exposures += 1
        self.candidates[candidate_id]["status"] = status


def run_isolated_cycle(*, seed: int) -> dict[str, Any]:
    """Deterministic CPU-only M4 fixture; it cannot alter an incumbent."""

    if type(seed) is not int:
        raise ContractError("isolated cycle seed must be an integer")
    sandbox = PolicySandbox(cpu_ms=200, memory_bytes=128 * 1024**2, output_bytes=4096)
    observation = {"seed": seed, "candidates": ["near", "warm"]}
    useful = sandbox.run(
        "def decide(observation):\n"
        "    return {'chosen': sorted(observation['candidates'])[0], 'reason': 'deterministic_fixture'}\n",
        observation,
    )
    invalid = sandbox.run(
        "import socket\n"
        "def decide(observation):\n    return socket.gethostname()\n",
        observation,
    )
    body = {
        "schema_version": 1,
        "kind": "isolated_cycle_report",
        "seed": seed,
        "candidates": {
            "useful": {
                "status": "evaluated_no_change" if useful.status == "ok" else "rejected",
                "sandbox_status": useful.status,
                "output": useful.output,
            },
            "invalid": {"status": "rejected", "sandbox_status": invalid.status},
        },
        "outcome": "no_change",
        "incumbent_changed": False,
        "qualification": "none",
        "live_activation": False,
    }
    digest = hashlib.sha256(
        json.dumps(body, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {**body, "release_evidence_sha256": digest}
