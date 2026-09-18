"""Read-only completeness accounting for opt-in adapter traces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import ContractError


MAX_TRACE_BYTES = 64 * 1024 * 1024


def _tokens(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{field} must contain non-empty strings")
    return tuple(value)


def _read_trace(path: Path) -> tuple[list[dict[str, Any]], int, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractError("configured trace file is unavailable") from exc
    if size > MAX_TRACE_BYTES:
        raise ContractError("configured trace file exceeds read-only report limit")
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    malformed = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(value, dict):
            malformed += 1
            continue
        rows.append(value)
    return rows, malformed, hashlib.sha256(raw).hexdigest()


def build_completeness_report(manifest: Any) -> dict[str, Any]:
    """Inspect declared traces without changing adapters, files, or routing state."""

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ContractError("trace manifest must be a schema_version 1 object")
    if manifest.get("kind") != "adapter_trace_manifest":
        raise ContractError("trace manifest has wrong kind")
    raw_adapters = manifest.get("adapters")
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise ContractError("trace manifest adapters must be a non-empty list")

    adapters: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_adapters:
        if not isinstance(raw, dict):
            raise ContractError("adapter declaration must be an object")
        adapter_id = raw.get("adapter_id")
        if not isinstance(adapter_id, str) or not adapter_id or adapter_id in seen_ids:
            raise ContractError("adapter_id must be non-empty and unique")
        seen_ids.add(adapter_id)
        enabled = raw.get("enabled")
        if type(enabled) is not bool:
            raise ContractError("adapter enabled must be boolean")
        trace_files = raw.get("trace_files")
        if not isinstance(trace_files, list) or any(not isinstance(path, str) for path in trace_files):
            raise ContractError("trace_files must be a list of paths")
        arrivals = set(_tokens(raw.get("arrival_events"), "arrival_events"))
        terminals = set(_tokens(raw.get("terminal_events"), "terminal_events"))
        uninstrumented = raw.get("known_uninstrumented_callers")
        if not isinstance(uninstrumented, list) or any(
            not isinstance(item, str) or not item for item in uninstrumented
        ):
            raise ContractError("known_uninstrumented_callers must be a string list")

        if not enabled:
            adapters.append(
                {
                    "adapter_id": adapter_id,
                    "status": "disabled",
                    "trace_file_count": 0,
                    "known_uninstrumented_callers": sorted(set(uninstrumented)),
                    "complete": False,
                }
            )
            continue

        rows: list[dict[str, Any]] = []
        malformed = 0
        digests: list[str] = []
        unavailable_files = 0
        for trace_path in trace_files:
            try:
                found, bad, digest = _read_trace(Path(trace_path))
            except ContractError:
                unavailable_files += 1
                continue
            rows.extend(found)
            malformed += bad
            digests.append(digest)

        event_types: set[str] = set()
        arrived: set[str] = set()
        terminal: set[str] = set()
        dropped = 0
        for row in rows:
            event_type = row.get("event_type", row.get("kind"))
            if isinstance(event_type, str):
                event_types.add(event_type)
            request_id = row.get("request_id")
            if isinstance(request_id, str) and event_type in arrivals:
                arrived.add(request_id)
            if isinstance(request_id, str) and event_type in terminals:
                terminal.add(request_id)
            if event_type == "evidence_gap" and type(row.get("dropped_count")) is int:
                dropped += max(0, row["dropped_count"])
        missing_types = sorted((arrivals | terminals) - event_types)
        missing_terminal = sorted(arrived - terminal)
        complete = not (
            not trace_files
            or unavailable_files
            or malformed
            or dropped
            or missing_types
            or missing_terminal
            or uninstrumented
        )
        adapters.append(
            {
                "adapter_id": adapter_id,
                "status": "complete" if complete else "incomplete",
                "trace_file_count": len(trace_files),
                "trace_sha256": sorted(digests),
                "records": len(rows),
                "malformed_records": malformed,
                "unavailable_trace_files": unavailable_files,
                "dropped_observation_events": dropped,
                "missing_event_types": missing_types,
                "missing_terminal_request_ids": missing_terminal,
                "known_uninstrumented_callers": sorted(set(uninstrumented)),
                "complete": complete,
            }
        )

    enabled_rows = [row for row in adapters if row["status"] != "disabled"]
    complete = bool(enabled_rows) and all(row["complete"] for row in enabled_rows)
    return {
        "schema_version": 1,
        "kind": "adapter_trace_completeness_report",
        "status": "complete_for_enabled_adapters" if complete else "insufficient_evidence",
        "enabled_adapter_count": len(enabled_rows),
        "disabled_adapter_count": len(adapters) - len(enabled_rows),
        "adapters": adapters,
        "extrapolated_missing_evidence": False,
        "live_routing_changed": False,
    }
