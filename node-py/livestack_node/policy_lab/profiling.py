"""Pure profiling-manifest expansion; this module cannot submit work."""

from __future__ import annotations

import itertools
import hashlib
from typing import Any

from .contracts import ContractError
from ..workloads.model import WorkloadError


REQUIRED_WORKLOADS = frozenset({"llm-27b", "asr", "tts"})


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ContractError(f"{field} must be an integer >= {minimum}")
    return value


def _tokens(value: Any, field: str, *, minimum: int = 1) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"{field} must contain at least {minimum} values")
    if any(not isinstance(item, str) or not item for item in value) or len(set(value)) != len(value):
        raise ContractError(f"{field} must contain unique non-empty strings")
    return tuple(value)


def plan_profile_matrix(manifest: Any) -> dict[str, Any]:
    """Validate and expand a plan, deliberately without workload-client access."""

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ContractError("profiling manifest must be a schema_version 1 object")
    if manifest.get("kind") != "profiling_manifest":
        raise ContractError("profiling manifest has wrong kind")
    domain_id = manifest.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id:
        raise ContractError("profiling domain_id is required")
    regions = _tokens(manifest.get("regions"), "regions", minimum=2)
    if len(regions) != 2:
        raise ContractError("initial profiling domain requires exactly two regions")
    raw_directions = manifest.get("directions")
    if not isinstance(raw_directions, list):
        raise ContractError("directions must be a list")
    directions = {
        tuple(item) for item in raw_directions
        if isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
    }
    required_directions = {(regions[0], regions[1]), (regions[1], regions[0])}
    if directions != required_directions:
        raise ContractError("profiling must cover both regional directions exactly")
    raw_hardware = manifest.get("region_hardware")
    if not isinstance(raw_hardware, dict) or set(raw_hardware) != set(regions):
        raise ContractError("region_hardware must cover each region exactly")
    region_hardware = {
        region: _tokens(raw_hardware[region], f"hardware revisions for {region}")
        for region in regions
    }

    windows = _positive_int(manifest.get("observation_windows"), "observation_windows")
    if windows < 3:
        raise ContractError("profiling requires at least three observation windows")
    duration = _positive_int(manifest.get("max_duration_seconds"), "max_duration_seconds")
    budget = manifest.get("resource_budget")
    required_budget = {"gpu_seconds", "cpu_seconds", "memory_bytes", "network_bytes"}
    if not isinstance(budget, dict) or set(budget) != required_budget:
        raise ContractError("resource budget must declare all bounded dimensions")
    for name, value in budget.items():
        _positive_int(value, f"resource budget {name}", allow_zero=name == "gpu_seconds")

    protected = manifest.get("protected_service")
    if not isinstance(protected, dict) or set(protected) != {
        "minimum_free_gpu_fraction", "max_added_queue_ms", "abort_on_active_stream_interference"
    }:
        raise ContractError("protected service constraints are incomplete")
    free = protected["minimum_free_gpu_fraction"]
    if isinstance(free, bool) or not isinstance(free, (int, float)) or not 0 <= free <= 1:
        raise ContractError("minimum_free_gpu_fraction must be in [0, 1]")
    _positive_int(protected["max_added_queue_ms"], "max_added_queue_ms", allow_zero=True)
    if type(protected["abort_on_active_stream_interference"]) is not bool:
        raise ContractError("abort_on_active_stream_interference must be boolean")

    workloads = manifest.get("workloads")
    if not isinstance(workloads, list):
        raise ContractError("workloads must be a list")
    names = {item.get("workload_class") for item in workloads if isinstance(item, dict)}
    if names != REQUIRED_WORKLOADS or len(workloads) != len(REQUIRED_WORKLOADS):
        raise ContractError("profiling must declare exactly 27B, ASR and TTS workloads")

    cells: list[dict[str, Any]] = []
    cold_preparations = 0
    for workload in sorted(workloads, key=lambda item: item["workload_class"]):
        shapes = _tokens(workload.get("shapes"), "workload shapes")
        cache_states = _tokens(workload.get("cache_states"), "cache states")
        model_revisions = _tokens(workload.get("model_revisions"), "model revisions")
        runtime_revisions = _tokens(workload.get("runtime_revisions"), "runtime revisions")
        concurrency = workload.get("concurrency")
        if not isinstance(concurrency, list) or not concurrency:
            raise ContractError("workload concurrency must be a non-empty list")
        for value in concurrency:
            _positive_int(value, "workload concurrency")
        if len(set(concurrency)) != len(concurrency):
            raise ContractError("workload concurrency values must be unique")
        samples = _positive_int(workload.get("minimum_samples_per_cell"), "minimum samples per cell")
        if samples < 100:
            raise ContractError("minimum samples per cell must be at least 100")
        cold = _positive_int(
            workload.get("minimum_cold_preparations"),
            "minimum cold preparations",
            allow_zero=True,
        )
        if "cold" in cache_states and cold < 20:
            raise ContractError("cold profile paths require at least 20 preparations")
        cold_preparations += cold * len(regions)
        for region in regions:
            for window, shape, parallelism, cache, model, runtime, hardware in itertools.product(
                range(windows), shapes, sorted(concurrency), cache_states,
                model_revisions, runtime_revisions, region_hardware[region],
            ):
                cells.append({
                    "cell_id": (
                        f"{workload['workload_class']}:{region}:w{window}:{shape}:c{parallelism}:"
                        f"{cache}:{model}:{runtime}:{hardware}"
                    ),
                    "workload_class": workload["workload_class"],
                    "requester_region": region,
                    "observation_window": window,
                    "shape": shape,
                    "concurrency": parallelism,
                    "cache_state": cache,
                    "model_revision": model,
                    "runtime_revision": runtime,
                    "hardware_revision": hardware,
                    "sample_target": samples,
                })
    return {
        "schema_version": 1,
        "kind": "profiling_plan",
        "domain_id": domain_id,
        "regions": list(regions),
        "directions": [list(item) for item in sorted(directions)],
        "region_hardware": {key: list(value) for key, value in region_hardware.items()},
        "resource_budget": dict(budget),
        "max_duration_seconds": duration,
        "protected_service": dict(protected),
        "cells": cells,
        "cell_count": len(cells),
        "planned_requests": sum(cell["sample_target"] for cell in cells),
        "planned_cold_preparations": cold_preparations,
        "submission_count": 0,
        "execution_status": "not_submitted",
        "authorization_required": True,
    }


def submit_profile_plan(
    plan: Any,
    authority: Any,
    *,
    handler: str,
    input_digest: str,
    max_jobs: int,
) -> dict[str, Any]:
    """Submit only through an injected authorized authority; never execute locally."""

    if not isinstance(plan, dict) or plan.get("kind") != "profiling_plan":
        raise ContractError("profile submission requires a profiling plan")
    if type(max_jobs) is not int or max_jobs <= 0 or max_jobs > len(plan.get("cells", [])):
        raise ContractError("max_jobs must be positive and no larger than the plan")
    if not isinstance(handler, str) or not handler:
        raise ContractError("authorized profiling handler is required")
    if not isinstance(input_digest, str) or len(input_digest) != 64:
        raise ContractError("profiling input digest must be SHA-256 text")
    job_ids: list[str] = []
    for cell in plan["cells"][:max_jobs]:
        cell_hash = hashlib.sha256(cell["cell_id"].encode("utf-8")).hexdigest()[:24]
        request = {
            "version": 1,
            "key": f"policy-lab-profile/{cell_hash}",
            "handler": handler,
            "input_digest": input_digest,
            "payload": {"domain_id": plan["domain_id"], "cell": cell},
            "need": {"gpu": 1, "cpu": 1, "memory_bytes": plan["resource_budget"]["memory_bytes"]},
            "admit": {"gpu": 1},
            "selector": {"region": cell["requester_region"]},
            "estimate_seconds": plan["max_duration_seconds"],
            "retain": True,
        }
        try:
            result = authority.submit(request)
        except WorkloadError as exc:
            return {
                "schema_version": 1,
                "kind": "profiling_submission_report",
                "status": "admission_refused",
                "submitted": len(job_ids),
                "job_ids": job_ids,
                "refusal": str(exc),
                "local_fallback_attempts": 0,
            }
        job_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(job_id, str) or not job_id:
            raise ContractError("workload authority returned no job id")
        job_ids.append(job_id)
    return {
        "schema_version": 1,
        "kind": "profiling_submission_report",
        "status": "submitted",
        "submitted": len(job_ids),
        "job_ids": job_ids,
        "local_fallback_attempts": 0,
    }
