"""A Harmony multimodal embedding node: EmbeddingGemma 2 behind ``POST /v1/embeddings``.

    python -m livestack_node.embedding.serve_gemma

llama-server (pinned build, GGUF + mmproj) does the inference; this process is
the Harmony side of it, the way ``harmony-llm`` wraps vLLM: it announces one
``class=embed`` unit, starts the engine on demand, stops it when idle, admits
work in two lanes and names every failure.

    require:class=embed,modality=text+image,model=google/embeddinggemma-2,quant=q8_0

Wire: OpenAI-shaped. ``input`` is a string, or a list whose items are strings or
``{"content": [{"type": "text", "text": ...} | {"type": "image_url",
"image_url": {"url": "data:image/..."}}]}`` (the llama.cpp extension; verified
against the engine on zz-joe). Only ``data:`` image URLs are accepted: the
engine would fetch an ``http(s)`` URL itself, which makes this node an SSRF
proxy. ``dimensions`` truncates and renormalises (128/256/512/768).

Admission: ``workload`` is ``query`` (default for plain text, at most 8 inputs)
or ``bulk`` (media, or big batches). Bulk has fewer slots than the engine, so a
frame-indexing run cannot starve a query; a full lane answers 503 ``busy``
naming the lane, never a wait that looks like latency.

Environment (all read at start):
  HARMONY_GEMMA_BIN         llama-server binary                       (required)
  HARMONY_GEMMA_MODEL       embeddinggemma-2 GGUF                     (required)
  HARMONY_GEMMA_MMPROJ      mmproj GGUF                               (required)
  HARMONY_GEMMA_MODEL_ID    the id requests name   default google/embeddinggemma-2
  HARMONY_GEMMA_QUANT       tag stored with vectors default q8_0
  HARMONY_GEMMA_DIM         default output dim      default 512
  HARMONY_GEMMA_BACKEND     cpu | vulkan            default cpu
  HARMONY_GEMMA_VK_DEVICE   Vulkan device index     default 0
  HARMONY_GEMMA_THREADS     cpu threads             default 4
  HARMONY_GEMMA_CTX         context = -b = -ub      default 8192
  HARMONY_GEMMA_PORT        facade port             default 8230
  HARMONY_GEMMA_UPSTREAM_PORT  engine port (127.0.0.1 only)  default 8231
  HARMONY_GEMMA_IDLE_SECONDS   default 900
  HARMONY_GEMMA_HOST_ID     default "<hostname>-gemma"
"""
from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import time
from typing import Dict, List, Optional

import numpy as np
from starlette.requests import Request

from livestack_node import attach, counting
from livestack_node.facade import _machine_name
from livestack_node.manager import ManagedUnit, ResidencyPolicy

DIMS = (128, 256, 512, 768)
MAX_INPUTS = 64
QUERY_MAX_INPUTS = 8
MAX_TEXT_CHARS = 32768
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
# The engine has 4 slots. Query may use all of them; bulk at most two, so two
# slots are always free for a query while a frame run saturates its own lane.
ENGINE_SLOTS = 4
LANE_LIMITS = {"query": ENGINE_SLOTS, "bulk": 2}
START_TIMEOUT_S = 120.0
LOAD_FAILURE_HOLD_S = 60.0
UPSTREAM_TIMEOUT_S = 300.0


class BadRequest(ValueError):
    """The request is malformed; ``kind`` becomes the error ``type``."""

    def __init__(self, message: str, kind: str = "invalid_request_error", status: int = 400):
        super().__init__(message)
        self.kind, self.status = kind, status


def normalise_inputs(raw) -> tuple[List[object], bool]:
    """Validate ``input`` and return (engine inputs, has_media).

    Strings pass through. A content item passes through only if every part is
    text or a ``data:image/`` URL within the byte bound; anything else is named.
    """
    items = [raw] if isinstance(raw, (str, dict)) else raw
    if not isinstance(items, list) or not items:
        raise BadRequest("input must be a string, a content item, or a non-empty list of them")
    if len(items) > MAX_INPUTS:
        raise BadRequest(f"at most {MAX_INPUTS} inputs per request", "request_too_large", 413)
    out: List[object] = []
    media = False
    for item in items:
        if isinstance(item, str):
            if len(item) > MAX_TEXT_CHARS:
                raise BadRequest(f"text input over {MAX_TEXT_CHARS} chars", "request_too_large", 413)
            out.append(item)
            continue
        parts = item.get("content") if isinstance(item, dict) else None
        if not isinstance(parts, list) or not parts:
            raise BadRequest("an input item must be a string or {\"content\": [parts]}")
        for part in parts:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text":
                if not isinstance(part.get("text"), str) or len(part["text"]) > MAX_TEXT_CHARS:
                    raise BadRequest("text part must be a string within the size bound")
            elif kind == "image_url":
                url = (part.get("image_url") or {}).get("url")
                if not isinstance(url, str) or not url.startswith("data:image/"):
                    raise BadRequest("image_url must be a data:image/ URL; remote URLs are not fetched here")
                if len(url) * 3 // 4 > MAX_IMAGE_BYTES:
                    raise BadRequest(f"image over {MAX_IMAGE_BYTES} bytes", "request_too_large", 413)
                media = True
            else:
                raise BadRequest(f"unsupported content part type {kind!r}; supported: text, image_url")
        out.append(item)
    return out, media


def pick_lane(requested: Optional[str], inputs: List[object], media: bool) -> str:
    if requested is not None:
        if requested not in LANE_LIMITS:
            raise BadRequest(f"workload must be one of {sorted(LANE_LIMITS)}")
        return requested
    return "bulk" if media or len(inputs) > QUERY_MAX_INPUTS else "query"


def shape_vectors(vectors: List[List[float]], dim: int) -> np.ndarray:
    """Truncate to ``dim`` and renormalise (Matryoshka-style truncation is only
    meaningful on a unit vector)."""
    matrix = np.asarray(vectors, dtype=np.float32)[:, :dim]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def encode_vectors(matrix: np.ndarray, encoding_format: str) -> List[object]:
    if encoding_format == "base64":
        rows = matrix.astype("<f4", copy=False)
        return [base64.b64encode(row.tobytes()).decode("ascii") for row in rows]
    return [row.tolist() for row in matrix]


class Engine:
    """One llama-server subprocess, bound to 127.0.0.1."""

    def __init__(self, argv: List[str], env: Dict[str, str], port: int):
        self.argv, self.env, self.port = argv, env, port
        self.proc: Optional[subprocess.Popen] = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Engine":
        import urllib.request
        self.proc = subprocess.Popen(self.argv, env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.proc.returncode} during start")
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                    if r.status == 200:
                        self._warm()
                        return self
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"llama-server not healthy within {START_TIMEOUT_S:.0f}s")

    def _warm(self) -> None:
        """One real embed before the unit counts as loaded: the first request on a
        Vulkan card took ~4.5 s (shader compile) against ~60 ms after, and that
        first request is otherwise a user's search query."""
        import json
        import urllib.request
        req = urllib.request.Request(self.base + "/v1/embeddings", method="POST",
                                     data=json.dumps({"model": "x", "input": "warm"}).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def engine_env(binary: str, base: Dict[str, str], *, backend: str, vk_device: str) -> Dict[str, str]:
    """The engine's environment.

    llama.cpp builds link their shared libraries by an ABSOLUTE RUNPATH into the
    build directory. A binary copied beside its libraries ran only while that
    build tree existed (and only from its own cwd): when the tree was deleted the
    engine died with exit 127 on its next idle reload, mid-service
    (2026-10-12). Putting the binary's own directory first makes any relocated
    engine directory work."""
    env = dict(base)
    libdir = os.path.dirname(os.path.abspath(binary))
    env["LD_LIBRARY_PATH"] = libdir + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    if backend == "vulkan":
        env["GGML_VK_VISIBLE_DEVICES"] = vk_device
    return env


def engine_argv(binary: str, model: str, mmproj: str, *, backend: str, threads: int, ctx: int,
                port: int) -> List[str]:
    argv = [binary, "-m", model, "--mmproj", mmproj, "--embedding", "--host", "127.0.0.1",
            "--port", str(port), "-c", str(ctx), "-b", str(ctx), "-ub", str(ctx),
            "--parallel", str(ENGINE_SLOTS), "-t", str(threads)]
    # -b/-ub must cover the longest input in tokens: the default 512 rejected a
    # 1049-token Chinese chunk with HTTP 500 (spike A2, 2026-10-12). No
    # --pooling: the GGUF carries the model's own.
    argv += ["-ngl", "99"] if backend == "vulkan" else ["-ngl", "0"]
    return argv


def create_app(*, port: Optional[int] = None):
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    def need(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is required")
        return value

    binary, model_path, mmproj = need("HARMONY_GEMMA_BIN"), need("HARMONY_GEMMA_MODEL"), need("HARMONY_GEMMA_MMPROJ")
    for path in (binary, model_path, mmproj):
        if not os.path.exists(path):
            raise RuntimeError(f"{path} does not exist")
    model_id = os.environ.get("HARMONY_GEMMA_MODEL_ID", "google/embeddinggemma-2")
    quant = os.environ.get("HARMONY_GEMMA_QUANT", "q8_0")
    default_dim = int(os.environ.get("HARMONY_GEMMA_DIM", "512"))
    if default_dim not in DIMS:
        raise RuntimeError(f"HARMONY_GEMMA_DIM must be one of {DIMS}")
    backend = os.environ.get("HARMONY_GEMMA_BACKEND", "cpu").strip().lower()
    if backend not in ("cpu", "vulkan"):
        raise RuntimeError("HARMONY_GEMMA_BACKEND must be cpu or vulkan")
    vk_device = os.environ.get("HARMONY_GEMMA_VK_DEVICE", "0")
    threads = int(os.environ.get("HARMONY_GEMMA_THREADS", "4"))
    ctx = int(os.environ.get("HARMONY_GEMMA_CTX", "8192"))
    port = port if port is not None else int(os.environ.get("HARMONY_GEMMA_PORT", "8230"))
    upstream_port = int(os.environ.get("HARMONY_GEMMA_UPSTREAM_PORT", "8231"))
    idle_seconds = int(os.environ.get("HARMONY_GEMMA_IDLE_SECONDS", "900"))
    host_id = os.environ.get("HARMONY_GEMMA_HOST_ID") or f"{socket.gethostname()}-gemma"

    env = engine_env(binary, dict(os.environ), backend=backend, vk_device=vk_device)
    argv = engine_argv(binary, model_path, mmproj, backend=backend, threads=threads, ctx=ctx,
                       port=upstream_port)
    engines: List[Engine] = []          # the live engine, if any
    failures: Dict[str, tuple] = {}
    busy = counting()
    lanes = {"query": 0, "bulk": 0}
    unit_name = "embed_embeddinggemma_2"

    def load() -> Engine:
        engine = Engine(argv, env, upstream_port).start()
        engines.append(engine)
        return engine

    def free() -> None:
        while engines:
            engines.pop().stop()

    attrs = {"class": "embed", "model": model_id, "quant": quant, "dim": default_dim,
             "modality": "text+image"}
    # Measured on zz-joe 2026-10-12: ~1.3 GB of VRAM (Vulkan), ~1 GB RSS on CPU.
    unit = ManagedUnit(unit_name, load, free, footprint=int(1.5 * 2**30),
                       residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
                       spread_group="embed", attributes=attrs, reload_cost=3.0,
                       health_check=lambda handle: handle.alive())
    holder: Dict[str, object] = {}

    def readiness() -> dict:
        at_msg = failures.get(unit_name)
        if at_msg and time.monotonic() - at_msg[0] < LOAD_FAILURE_HOLD_S:
            return {"ready": False, "detail": f"load failed: {at_msg[1]}"}
        return {"ready": True,
                "detail": "engine resident" if unit.loaded else "engine not resident; starts on demand"}

    app = FastAPI()

    def ensure_engine() -> Engine:
        try:
            engine = holder["manager"].ensure(unit_name)
        except Exception as exc:  # noqa: BLE001 - reported to the caller and in readiness
            failures[unit_name] = (time.monotonic(), f"{type(exc).__name__}: {exc}")
            raise
        failures.pop(unit_name, None)
        return engine

    def error(message: str, kind: str, status: int):
        return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request, body: dict):
        if body.get("model") not in (model_id, unit_name):
            return error(f"model {body.get('model')!r} is not served here; served: {model_id!r}",
                         "model_not_found", 404)
        declared = request.headers.get("content-length")
        if declared and int(declared) > MAX_BODY_BYTES:
            return error(f"request over {MAX_BODY_BYTES} bytes", "request_too_large", 413)
        encoding = body.get("encoding_format") or "float"
        if encoding not in ("float", "base64"):
            return error("encoding_format must be float or base64", "invalid_request_error", 400)
        dim = body.get("dimensions", default_dim)
        if dim not in DIMS:
            return error(f"dimensions must be one of {list(DIMS)}", "invalid_request_error", 400)
        try:
            inputs, media = normalise_inputs(body.get("input"))
            lane = pick_lane(body.get("workload"), inputs, media)
        except BadRequest as exc:
            return error(str(exc), exc.kind, exc.status)
        if lanes[lane] >= LANE_LIMITS[lane]:
            return error(f"{lane} lane full ({LANE_LIMITS[lane]} requests)", "busy", 503)
        lanes[lane] += 1
        try:
            with busy:
                try:
                    engine = await asyncio.to_thread(ensure_engine)
                except Exception as exc:  # noqa: BLE001 - explicit load degradation
                    return error(f"{unit_name} could not load: {exc}", "unavailable", 503)
                started = time.monotonic()
                # Cancelling this handler (client gone) closes the upstream
                # connection, and the engine cancels the task.
                async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S) as client:
                    try:
                        reply = await client.post(engine.base + "/v1/embeddings",
                                                  json={"model": "x", "input": inputs})
                    except httpx.HTTPError as exc:
                        failures[unit_name] = (time.monotonic(), f"engine unreachable: {exc!r}")
                        return error(f"engine unreachable: {exc!r}", "unavailable", 503)
                if reply.status_code != 200:
                    # The engine's own message, named: e.g. "Failed to load image".
                    try:
                        detail = reply.json()["error"]["message"]
                    except Exception:  # noqa: BLE001
                        detail = reply.text[:200]
                    return error(f"engine {reply.status_code}: {detail}", "engine_error",
                                 400 if reply.status_code < 500 or "image" in detail.lower() else 502)
                data = sorted(reply.json()["data"], key=lambda d: d["index"])
                matrix = shape_vectors([d["embedding"] for d in data], dim)
                compute_ms = (time.monotonic() - started) * 1000
        finally:
            lanes[lane] -= 1
        payload = [{"object": "embedding", "index": i, "embedding": v}
                   for i, v in enumerate(encode_vectors(matrix, encoding))]
        return JSONResponse({"object": "list", "data": payload, "model": model_id,
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}},
                            headers={"x-harmony-unit": unit_name, "x-compute-ms": f"{compute_ms:.1f}",
                                     "x-harmony-lane": lane})

    attach_kwargs = {}
    if backend == "vulkan":
        # No CUDA/MLX meter reads a Vulkan card; report none rather than a wrong one.
        # The device id is `{machine}/vk<n>`, NOT `{host_id}/...`: the hub's fleet
        # roster (plugins-v2/harmony.fleet-source) keeps a node whose host id is not
        # the vantage host's only when its device id starts with `{machine}/`, so a
        # `zz-joe-gemma/vk1` node was silently absent from the hub's view and
        # every Gemma request answered "no fleet node satisfies" (2026-10-12).
        attach_kwargs = {"device_meter": None, "device_id": f"{_machine_name(host_id)}/vk{vk_device}"}
    manager, coordinator = attach(
        app, host_id=host_id, kind="embed", units={unit_name: unit}, idle_seconds=idle_seconds,
        coload=True, gpu_call=lambda fn: fn(), port=port, in_flight=busy, readiness=readiness,
        backend=backend, preload=[unit_name], **attach_kwargs)
    holder["manager"] = manager
    app.state.embed_manager = manager
    app.state.embed_coordinator = coordinator
    return app


def main():
    import uvicorn
    port = int(os.environ.get("HARMONY_GEMMA_PORT", "8230"))
    uvicorn.run(create_app(port=port), host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
