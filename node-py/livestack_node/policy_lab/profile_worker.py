"""Installed workload handler for bounded, metadata-only engine profiling."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import urllib.request
import uuid
import wave

from .contracts import ContractError


def _json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:
        value = json.loads(response.read(1_000_001))
    if not isinstance(value, dict):
        raise ContractError("engine metadata response must be an object")
    return value


def _protected(endpoint: str, protected: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    residence = _json(endpoint + "/livestack/residence")
    health = _json(endpoint + "/health")
    if protected.get("abort_on_active_stream_interference"):
        if health.get("inflight", 0) not in (0, None):
            raise ContractError("protected endpoint is busy")
        if any(unit.get("busy") for unit in residence.get("units", []) if isinstance(unit, dict)):
            raise ContractError("protected endpoint is busy")
    return residence, health


def _shape_number(shape: str, pattern: str) -> int:
    match = re.fullmatch(pattern, shape)
    if match is None:
        raise ContractError(f"unsupported profiling shape: {shape}")
    return int(match.group(1))


def _post(url: str, body: bytes, content_type: str, *, timeout: int = 900) -> dict[str, int]:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    started = time.monotonic_ns()
    first = None
    received = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            if first is None:
                first = time.monotonic_ns()
            received += len(chunk)
    finished = time.monotonic_ns()
    first = finished if first is None else first
    return {
        "first_output_us": (first - started) // 1_000,
        "completion_us": (finished - started) // 1_000,
        "request_bytes": len(body),
        "response_bytes": received,
    }


def _tts(endpoint: str, cell: dict[str, Any], voice_id: str, sample: int) -> dict[str, int]:
    chars = _shape_number(cell["shape"], r"text-(\d+)-chars")
    marker = f" sample {sample} "
    text = ("Profile sentence " + marker + "x" * chars)[:chars]
    body = json.dumps({"text": text, "voice_id": voice_id, "engine": "voxcpm"}).encode()
    return _post(endpoint + "/tts/stream", body, "application/json")


def _wav(seconds: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16_000)
        wav.writeframes(b"\0\0" * 16_000 * seconds)
    return out.getvalue()


def _asr(endpoint: str, cell: dict[str, Any], _voice_id: str, sample: int) -> dict[str, int]:
    seconds = _shape_number(cell["shape"], r"stream-(\d+)s")
    audio = _wav(seconds)
    boundary = "policy-lab-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample-{sample}.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    return _post(
        endpoint + "/v1/audio/transcriptions", body,
        f"multipart/form-data; boundary={boundary}",
    )


def _llm(endpoint: str, cell: dict[str, Any], _voice_id: str, sample: int) -> dict[str, int]:
    match = re.fullmatch(r"prompt-(\d+)-output-(\d+)", cell["shape"])
    if match is None:
        raise ContractError(f"unsupported profiling shape: {cell['shape']}")
    prompt_tokens, output_tokens = map(int, match.groups())
    prompt = (f"profile-{sample} " + "token " * prompt_tokens)[: prompt_tokens * 6]
    body = json.dumps({
        "model": "llm_general", "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_tokens, "temperature": 0, "stream": True,
    }).encode()
    return _post(endpoint + "/v1/chat/completions", body, "application/json")


RUNNERS = {"llm-27b": _llm, "asr": _asr, "tts": _tts}
ENGINE_UNITS = {"llm-27b": "llm_general", "asr": "asr", "tts": "voxcpm"}


def run_heldout_episodes(
    endpoints: Any,
    *,
    execution_targets: Any,
    protected: Any,
    voice_id: str,
    episodes: int,
) -> dict[str, Any]:
    """Collect independent incumbent episodes; prediction happens offline."""

    if type(episodes) is not int or not 1 <= episodes <= 100:
        raise ContractError("held-out episode count must be in [1, 100]")
    if not isinstance(execution_targets, dict) or set(execution_targets) != set(RUNNERS):
        raise ContractError("held-out execution targets must cover all workloads")
    if not voice_id:
        raise ContractError("TTS profiling requires an explicit voice identity")
    resolved: dict[str, str] = {}
    for workload, target in execution_targets.items():
        key = f"{target}:{workload}"
        if not isinstance(endpoints, dict) or not isinstance(endpoints.get(key), str):
            raise ContractError("held-out execution target has no installed endpoint")
        resolved[workload] = endpoints[key].rstrip("/")
        residence, _ = _protected(resolved[workload], protected)
        if not any(
            unit.get("kind") == ENGINE_UNITS[workload] and unit.get("resident") is True
            for unit in residence.get("units", []) if isinstance(unit, dict)
        ):
            raise ContractError("held-out episode requires already resident engines")

    requests: list[dict[str, Any]] = []
    origin = time.monotonic_ns()
    shapes = {"asr": "stream-5s", "llm-27b": "prompt-512-output-128", "tts": "text-20-chars"}
    for episode in range(episodes):
        workflow = f"episode-{episode}"
        completions: dict[str, int] = {}
        for workload in ("asr", "llm-27b", "tts"):
            endpoint = resolved[workload]
            _protected(endpoint, protected)
            request_id = f"{workflow}-{'llm' if workload == 'llm-27b' else workload}"
            if workload == "tts":
                arrival = {
                    "kind": "after_dependencies",
                    "dependency_request_ids": [f"{workflow}-llm"],
                    "think_time_us": 50_000,
                }
                time.sleep(0.05)
            else:
                arrival = {"kind": "external", "relative_time_us": (time.monotonic_ns() - origin) // 1_000}
            started_us = (time.monotonic_ns() - origin) // 1_000
            cell = {"shape": shapes[workload]}
            result = RUNNERS[workload](endpoint, cell, voice_id, episode)
            completion_us = started_us + result["completion_us"]
            completions[request_id] = completion_us
            requests.append({
                "request_id": request_id,
                "workflow_id": workflow,
                "workload_class": workload,
                "arrival": arrival,
                "observed_start_us": started_us,
                "observed_completion_us": completion_us,
                "observed_first_output_us": started_us + result["first_output_us"],
                "observed_external_occupancy": 0,
                "observed_state": "resident_warm",
                "execution_target": execution_targets[workload],
            })
    return {
        "schema_version": 1,
        "kind": "heldout_observation_pack",
        "status": "complete",
        "episode_count": episodes,
        "requests": requests,
        "metadata_only": True,
        "contains_predictions": False,
        "local_or_unreserved_fallback_attempts": 0,
    }


def run_profile_cell(
    cell: Any,
    endpoints: Any,
    *,
    protected: Any,
    voice_id: str,
) -> dict[str, Any]:
    if not isinstance(cell, dict) or cell.get("workload_class") not in RUNNERS:
        raise ContractError("invalid profiling cell")
    if cell.get("cache_state") != "warm":
        raise ContractError("installed profiling handler currently permits warm cells only")
    endpoint_key = f"{cell.get('execution_target')}:{cell.get('workload_class')}"
    if not isinstance(endpoints, dict) or not isinstance(endpoints.get(endpoint_key), str):
        raise ContractError("execution target has no installed endpoint")
    if not isinstance(protected, dict):
        raise ContractError("protected service constraints are required")
    sample_target = cell.get("sample_target")
    concurrency = cell.get("concurrency")
    if type(sample_target) is not int or not 1 <= sample_target <= 10_000:
        raise ContractError("sample_target is outside installed handler bounds")
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ContractError("concurrency is outside installed handler bounds")
    if cell["workload_class"] == "tts" and (not isinstance(voice_id, str) or not voice_id):
        raise ContractError("TTS profiling requires an explicit voice identity")
    endpoint = endpoints[endpoint_key].rstrip("/")
    before_residence, before_health = _protected(endpoint, protected)
    expected_unit = ENGINE_UNITS[cell["workload_class"]]
    if not any(
        unit.get("kind") == expected_unit and unit.get("resident") is True
        for unit in before_residence.get("units", []) if isinstance(unit, dict)
    ):
        raise ContractError("warm profiling cell requires an already resident engine")
    runner = RUNNERS[cell["workload_class"]]
    samples: list[dict[str, int]] = []
    for start in range(0, sample_target, concurrency):
        _protected(endpoint, protected)
        indexes = range(start, min(start + concurrency, sample_target))
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples.extend(pool.map(lambda index: runner(endpoint, cell, voice_id, index), indexes))
    after_residence, after_health = _protected(endpoint, protected)
    return {
        "schema_version": 1,
        "kind": "measured_profile_pack",
        "status": "complete",
        "cell": cell,
        "samples": samples,
        "sample_count": len(samples),
        "metadata_only": True,
        "local_or_unreserved_fallback_attempts": 0,
        "engine_observation": {
            "before": {"residence": before_residence, "health": before_health},
            "after": {"residence": after_residence, "health": after_health},
        },
    }


def main() -> int:
    request = json.loads(Path(os.environ["HARMONY_REQUEST"]).read_text(encoding="utf-8"))
    endpoints = json.loads(os.environ["POLICY_LAB_ENDPOINTS"])
    output = Path(os.environ["HARMONY_OUTPUT"])
    if request.get("kind") == "heldout_episode_manifest":
        pack = run_heldout_episodes(
            endpoints,
            execution_targets=request["execution_targets"],
            protected=request["protected_service"],
            voice_id=os.environ.get("POLICY_LAB_TTS_VOICE", ""),
            episodes=request["episodes"],
        )
        (output / "heldout-observations.json").write_text(
            json.dumps(pack, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    pack = run_profile_cell(
        request["cell"], endpoints,
        protected=request["protected_service"],
        voice_id=os.environ.get("POLICY_LAB_TTS_VOICE", ""),
    )
    (output / "profile-pack.json").write_text(
        json.dumps(pack, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
