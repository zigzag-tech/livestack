"""Offline command family for validation, replay and policy comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import ContractError, load_json
from .completeness import build_completeness_report
from .calibration import evaluate_calibration
from .cycle import plan_cycle, run_isolated_cycle, submit_cycle
from ..workloads.client import WorkloadClient
from .policies import (
    RoutingCandidate,
    RoutingRequest,
    least_queue,
    nearest_ready,
    total_latency_demand_aware,
    warm_first,
)
from .overhead import measure_observer_overhead
from .profiling import plan_profile_matrix
from .promotion import CellComparison, evaluate_promotion
from .smoke import replay_smoke


def _read(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> Any:
    return load_json(path.read_bytes(), max_bytes=max_bytes)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("manifest must be an object")
    if value.get("schema_version") != 1:
        raise ContractError("unsupported schema_version")
    if not isinstance(value.get("kind"), str) or not value["kind"]:
        raise ContractError("manifest kind is required")
    return value


def _write_report(out: Path, report: dict[str, Any], markdown: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(markdown, encoding="utf-8")


def _routing_policy(name: str):
    try:
        return {
            "nearest-ready": nearest_ready,
            "warm-first": warm_first,
            "least-queue": least_queue,
            "total-latency-demand-aware": total_latency_demand_aware,
        }[name]
    except KeyError as exc:
        raise ContractError(f"unknown replay policy: {name}") from exc


def _replay(args: argparse.Namespace) -> int:
    dataset = _base_validate(_read(args.dataset))
    profiles = _base_validate(_read(args.profiles))
    if dataset["kind"] != "routing_dataset" or profiles["kind"] not in {"synthetic_profiles", "performance_profiles"}:
        raise ContractError("replay received wrong artifact kinds")
    decisions = []
    policy = _routing_policy(args.policy)
    for raw in dataset.get("requests", []):
        request_fields = {name: raw[name] for name in RoutingRequest.__dataclass_fields__}
        request = RoutingRequest(**request_fields)
        candidates = tuple(RoutingCandidate(**value) for value in raw["candidates"])
        decision = policy(request, candidates)
        decisions.append(
            {
                "request_id": request.request_id,
                "chosen_worker_id": decision.chosen_worker_id,
                "reason_code": decision.reason_code,
                "candidate_totals_us": {
                    trace.worker_id: trace.total_upper_us for trace in decision.candidates
                },
            }
        )
    report = {
        "schema_version": 1,
        "kind": "replay_report",
        "dataset_sha256": _hash(args.dataset),
        "profiles_sha256": _hash(args.profiles),
        "policy": args.policy,
        "seed": args.seed,
        "qualification": "none",
        "decisions": decisions,
    }
    _write_report(
        args.out,
        report,
        "# Replay report\n\nThis is an uncalibrated diagnostic replay. It makes no live-performance claim.\n",
    )
    return 0


def _compare(args: argparse.Namespace) -> int:
    benchmark = _base_validate(_read(args.benchmark))
    if benchmark["kind"] != "comparison_benchmark":
        raise ContractError("compare requires comparison_benchmark")
    candidate_names = tuple(name for name in args.candidates.split(",") if name)
    results = {}
    statuses = set()
    for name in candidate_names:
        raw = benchmark["candidates"].get(name)
        if raw is None:
            raise ContractError(f"benchmark has no candidate: {name}")
        cells = tuple(CellComparison(**cell) for cell in raw["cells"])
        result = evaluate_promotion(
            cells,
            claimed_primary=raw["claimed_primary"],
            calibrated=benchmark["calibrated"],
            invariants_pass=benchmark["invariants_pass"],
            uncertainty_invariants=tuple(benchmark["uncertainty_invariants"]),
        )
        results[name] = {"status": result.status, "reasons": list(result.reasons)}
        statuses.add(result.status)
    report = {
        "schema_version": 1,
        "kind": "comparison_report",
        "benchmark_sha256": _hash(args.benchmark),
        "results": results,
    }
    _write_report(
        args.out,
        report,
        "# Comparison report\n\n" + "\n".join(f"- {name}: {value['status']}" for name, value in sorted(results.items())) + "\n",
    )
    if "regression" in statuses:
        return 3
    if "insufficient_evidence" in statuses:
        return 4
    return 0


def _observer_overhead(args: argparse.Namespace) -> int:
    report = measure_observer_overhead(samples=args.samples, warmup=args.warmup)
    checks = "\n".join(
        f"- {name}: {'pass' if passed else 'fail'}"
        for name, passed in sorted(report["checks"].items())
    )
    _write_report(
        args.out,
        report,
        "# Observer overhead report\n\n"
        "This is an isolated streaming-callback measurement. It makes no live service "
        "performance or routing-readiness claim.\n\n"
        f"Status: **{report['status']}**\n\n{checks}\n",
    )
    return {"passed": 0, "failed": 3, "insufficient_evidence": 4}[report["status"]]


def _completeness(args: argparse.Namespace) -> int:
    report = build_completeness_report(_read(args.manifest))
    rows = "\n".join(
        f"- {adapter['adapter_id']}: {adapter['status']}"
        for adapter in report["adapters"]
    )
    _write_report(
        args.out,
        report,
        "# Adapter trace completeness\n\n"
        "Read-only evidence inventory; missing records and callers are not extrapolated.\n\n"
        f"Status: **{report['status']}**\n\n{rows}\n",
    )
    return 0 if report["status"] == "complete_for_enabled_adapters" else 4


def _profile_plan(args: argparse.Namespace) -> int:
    report = plan_profile_matrix(_read(args.manifest))
    _write_report(
        args.out,
        report,
        "# Profiling plan\n\n"
        "Planning only: no request was submitted and authorization is still required.\n\n"
        f"- Domain: {report['domain_id']}\n"
        f"- Matrix cells: {report['cell_count']}\n"
        f"- Planned requests: {report['planned_requests']}\n"
        f"- Submission count: {report['submission_count']}\n",
    )
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    observations = _base_validate(_read(args.observations))
    profiles = _base_validate(_read(args.profiles))
    if profiles["kind"] != "performance_profiles":
        raise ContractError("calibrate requires performance_profiles")
    if observations.get("profile_sha256") != _hash(args.profiles):
        raise ContractError("calibration dataset profile hash does not match profiles")
    report = evaluate_calibration(observations)
    _write_report(
        args.out,
        report,
        "# Calibration report\n\n"
        f"Status: **{report['status']}** for `{report['domain_id']}`.\n\n"
        "Only the named domain and exact profile hash can receive a certificate.\n",
    )
    return {"calibrated": 0, "failed": 3, "insufficient_evidence": 4}[report["status"]]


def _cycle_client(config_path: Path) -> WorkloadClient:
    config = _read(config_path, max_bytes=65_536)
    if not isinstance(config, dict):
        raise ContractError("authority config must be an object")
    return WorkloadClient(config["authority"], config["token"], timeout=60)


def _cycle(args: argparse.Namespace) -> int:
    if args.cycle_command == "plan":
        report = plan_cycle(_read(args.config))
        _write_report(
            args.out,
            report,
            "# Periodic policy cycle plan\n\n"
            "Planning does not submit work or install a scheduler.\n\n"
            f"- Cycle: {report['cycle_id']}\n- Mode: {report['mode']}\n",
        )
        return 0
    if args.cycle_command == "fixture":
        report = run_isolated_cycle(seed=args.seed)
        _write_report(
            args.out,
            report,
            "# Isolated periodic-cycle fixture\n\n"
            "CPU-only, no live activation. The incumbent is unchanged and the outcome is no-change.\n",
        )
        return 0
    client = _cycle_client(args.authority_config)
    if args.cycle_command == "submit":
        plan = _base_validate(_read(args.manifest))
        result = submit_cycle(
            plan, client, handler=args.handler, input_digest=args.input_digest
        )
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 0
    result = client.get(args.job_id)
    if args.cycle_command == "status":
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 0
    report = {
        "schema_version": 1,
        "kind": "cycle_durable_report",
        "job_id": args.job_id,
        "state": result.get("state"),
        "result": result.get("result"),
    }
    _write_report(
        args.out,
        report,
        f"# Durable cycle report\n\nJob `{args.job_id}` state: **{report['state']}**.\n",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m livestack_node.policy_lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    smoke = subparsers.add_parser("replay-smoke")
    smoke.add_argument("manifest", type=Path)
    replay = subparsers.add_parser("replay")
    replay.add_argument("--dataset", type=Path, required=True)
    replay.add_argument("--profiles", type=Path, required=True)
    replay.add_argument("--policy", required=True)
    replay.add_argument("--seed", type=int, required=True)
    replay.add_argument("--out", type=Path, required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--benchmark", type=Path, required=True)
    compare.add_argument("--candidates", required=True)
    compare.add_argument("--out", type=Path, required=True)
    overhead = subparsers.add_parser("observer-overhead")
    overhead.add_argument("--samples", type=int, default=5_000)
    overhead.add_argument("--warmup", type=int, default=200)
    overhead.add_argument("--out", type=Path, required=True)
    completeness = subparsers.add_parser("completeness")
    completeness.add_argument("--manifest", type=Path, required=True)
    completeness.add_argument("--out", type=Path, required=True)
    profile_plan = subparsers.add_parser("profile-plan")
    profile_plan.add_argument("--manifest", type=Path, required=True)
    profile_plan.add_argument("--out", type=Path, required=True)
    calibrate = subparsers.add_parser("calibrate")
    calibrate.add_argument("--observations", type=Path, required=True)
    calibrate.add_argument("--profiles", type=Path, required=True)
    calibrate.add_argument("--out", type=Path, required=True)
    cycle = subparsers.add_parser("cycle")
    cycle_commands = cycle.add_subparsers(dest="cycle_command", required=True)
    cycle_plan = cycle_commands.add_parser("plan")
    cycle_plan.add_argument("--config", type=Path, required=True)
    cycle_plan.add_argument("--out", type=Path, required=True)
    cycle_submit = cycle_commands.add_parser("submit")
    cycle_submit.add_argument("--manifest", type=Path, required=True)
    cycle_submit.add_argument("--authority-config", type=Path, required=True)
    cycle_submit.add_argument("--handler", default="policy_lab_cycle")
    cycle_submit.add_argument("--input-digest", required=True)
    cycle_fixture = cycle_commands.add_parser("fixture")
    cycle_fixture.add_argument("--seed", type=int, required=True)
    cycle_fixture.add_argument("--out", type=Path, required=True)
    cycle_status = cycle_commands.add_parser("status")
    cycle_status.add_argument("job_id")
    cycle_status.add_argument("--authority-config", type=Path, required=True)
    cycle_report = cycle_commands.add_parser("report")
    cycle_report.add_argument("job_id")
    cycle_report.add_argument("--authority-config", type=Path, required=True)
    cycle_report.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            _base_validate(_read(args.manifest))
            return 0
        if args.command == "replay-smoke":
            print(json.dumps(replay_smoke(_read(args.manifest)), sort_keys=True))
            return 0
        if args.command == "replay":
            return _replay(args)
        if args.command == "compare":
            return _compare(args)
        if args.command == "observer-overhead":
            return _observer_overhead(args)
        if args.command == "completeness":
            return _completeness(args)
        if args.command == "profile-plan":
            return _profile_plan(args)
        if args.command == "calibrate":
            return _calibrate(args)
        if args.command == "cycle":
            return _cycle(args)
        raise ContractError(f"unsupported command: {args.command}")
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
