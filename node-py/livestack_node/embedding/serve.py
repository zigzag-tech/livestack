"""A Harmony embedding node on the CPU: ``POST /v1/embeddings``, OpenAI-shaped.

    python -m livestack_node.embedding.serve

Each model is one unit with ``class=embed``, ``model=<hub id>``, ``quant`` and
``dim``; ``attach(backend="cpu")`` adds ``backend=cpu`` and puts the node on
``{machine}/cpu``, so a consumer can ask for

    require:class=embed,backend=cpu,model=Xenova/paraphrase-multilingual-MiniLM-L12-v2,quant=q8

and nothing it matches can take room on a card. GPU is the scarce resource on
this fleet; a 384-dimension MiniLM answers in milliseconds on a CPU.

Environment (all optional except the models):

  HARMONY_EMBED_MODELS        comma-separated Hugging Face ids (ONNX exports
                              with tokenizer.json, e.g. the Xenova ones)
  HARMONY_EMBED_QUANT         q8 (default) | fp32
  HARMONY_EMBED_THREADS       intra-op threads per model (default 4)
  HARMONY_EMBED_IDLE_SECONDS  unload a model idle this long (default 900)
  HARMONY_EMBED_PORT          default 8220
  HARMONY_EMBED_HOST_ID       default "<hostname>-embed"
  HF_HOME / HF_HUB_OFFLINE    where the model files are; offline after deploy

Announcing to brokers uses the usual LIVESTACK_* variables (see serve.attach).
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import threading
import time
from typing import Dict, List, Optional

from livestack_node import attach, counting
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .runtime import ONNX_FILES, OnnxSentenceEmbedder, model_files

# Request bounds. A caller's backlog must arrive as several requests, not one
# that holds the node: the hub sends at most 64 texts of at most 2,000 chars.
MAX_INPUTS = 256
MAX_INPUT_CHARS = 8192
# A failed load is retried, but the node says it is not ready meanwhile, so the
# fleet view routes around it instead of handing out an endpoint that errors.
LOAD_FAILURE_HOLD_S = 60.0
SWEEP_INTERVAL_S = 30.0


def unit_name(model_id: str) -> str:
    """`Xenova/paraphrase-multilingual-MiniLM-L12-v2` -> `embed_paraphrase_multilingual_minilm_l12_v2`."""
    tail = model_id.rsplit("/", 1)[-1].lower()
    return "embed_" + re.sub(r"[^a-z0-9]+", "_", tail).strip("_")


def resolve_model_dir(model_id: str, quant: str) -> str:
    """The local snapshot holding this model's files (downloads unless HF_HUB_OFFLINE)."""
    from huggingface_hub import snapshot_download
    return snapshot_download(model_id, allow_patterns=[
        ONNX_FILES[quant], "tokenizer.json", "tokenizer_config.json", "config.json",
        "special_tokens_map.json"])


def declared_dim(model_dir: str) -> Optional[int]:
    """Output dimension, read from the model's own config — derived, not declared."""
    try:
        with open(os.path.join(model_dir, "config.json")) as fh:
            size = json.load(fh).get("hidden_size")
        return int(size) if size else None
    except (OSError, ValueError, TypeError):
        return None


def encode_vectors(matrix, encoding_format: str) -> List[object]:
    """OpenAI's two encodings. base64 is little-endian float32 bytes — 4 bytes a
    float before base64's 4/3, against ~20 for a JSON decimal."""
    if encoding_format == "base64":
        rows = matrix.astype("<f4", copy=False)
        return [base64.b64encode(row.tobytes()).decode("ascii") for row in rows]
    return [row.tolist() for row in matrix]


def create_app(models: Optional[List[str]] = None, *, model_dirs: Optional[Dict[str, str]] = None,
               port: Optional[int] = None):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    models = models if models is not None else [
        m.strip() for m in os.environ.get("HARMONY_EMBED_MODELS", "").split(",") if m.strip()]
    if not models:
        raise RuntimeError("HARMONY_EMBED_MODELS names no model")
    quant = os.environ.get("HARMONY_EMBED_QUANT", "q8").strip()
    if quant not in ONNX_FILES:
        raise RuntimeError(f"HARMONY_EMBED_QUANT must be one of {sorted(ONNX_FILES)}")
    threads = int(os.environ.get("HARMONY_EMBED_THREADS", "4"))
    idle_seconds = int(os.environ.get("HARMONY_EMBED_IDLE_SECONDS", "900"))
    port = port if port is not None else int(os.environ.get("HARMONY_EMBED_PORT", "8220"))
    host_id = os.environ.get("HARMONY_EMBED_HOST_ID") or f"{socket.gethostname()}-embed"

    dirs = dict(model_dirs or {})
    for model_id in models:
        if model_id not in dirs:
            dirs[model_id] = resolve_model_dir(model_id, quant)
        model_files(dirs[model_id], quant)          # refuse to start on a missing file

    busy = counting()
    failures: Dict[str, tuple] = {}                 # unit -> (monotonic time, message)
    by_model: Dict[str, str] = {}
    units: Dict[str, ManagedUnit] = {}
    for model_id in models:
        name = unit_name(model_id)
        by_model[model_id] = name
        path = dirs[model_id]
        onnx_bytes = os.path.getsize(model_files(path, quant)["onnx"])
        attrs = {"class": "embed", "model": model_id, "quant": quant}
        dim = declared_dim(path)
        if dim:
            attrs["dim"] = dim
        units[name] = ManagedUnit(
            name, (lambda p=path: OnnxSentenceEmbedder(p, quant=quant, threads=threads)),
            lambda: None,
            # Resident cost measured on zz-joe (2026-10-01): weights plus the
            # session's working set came to ~2.5x the ONNX file.
            footprint=int(onnx_bytes * 2.5),
            residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
            spread_group="embed", attributes=attrs, reload_cost=1.0)

    def readiness() -> dict:
        now = time.monotonic()
        recent = {n: msg for n, (at, msg) in failures.items() if now - at < LOAD_FAILURE_HOLD_S}
        if recent:
            return {"ready": False,
                    "detail": "load failed: " + "; ".join(f"{n}: {m}" for n, m in sorted(recent.items()))}
        resident = [n for n, u in units.items() if u.loaded]
        return {"ready": True,
                "detail": ("resident: " + ", ".join(sorted(resident))) if resident
                else "no model resident; loads on demand (~1 s)"}

    app = FastAPI()
    holder: Dict[str, object] = {}

    def model_for(name: str):
        # ensure() serializes loads under the manager's own guard and restarts
        # the idle clock; a 1 s batch cannot outlive a 900 s idle window, so the
        # model is not evicted under a running request.
        try:
            model = holder["manager"].ensure(name)
        except Exception as exc:  # noqa: BLE001 - reported to the caller and in readiness
            failures[name] = (time.monotonic(), f"{type(exc).__name__}: {exc}")
            raise
        failures.pop(name, None)
        return model

    @app.post("/v1/embeddings")
    def embeddings(body: dict):
        requested = body.get("model")
        name = by_model.get(requested) or (requested if requested in units else None)
        if name is None:
            return JSONResponse({"error": {
                "message": f"model {requested!r} is not served here; served: {sorted(by_model)}",
                "type": "model_not_found"}}, status_code=404)
        raw = body.get("input")
        texts = [raw] if isinstance(raw, str) else raw
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return JSONResponse({"error": {"message": "input must be a string or a list of strings",
                                           "type": "invalid_request_error"}}, status_code=400)
        if len(texts) > MAX_INPUTS or any(len(t) > MAX_INPUT_CHARS for t in texts):
            return JSONResponse({"error": {
                "message": f"at most {MAX_INPUTS} inputs of at most {MAX_INPUT_CHARS} chars per request",
                "type": "request_too_large"}}, status_code=413)
        encoding = body.get("encoding_format") or "float"
        if encoding not in ("float", "base64"):
            return JSONResponse({"error": {"message": "encoding_format must be float or base64",
                                           "type": "invalid_request_error"}}, status_code=400)
        with busy:
            try:
                model = model_for(name)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse({"error": {"message": f"{name} could not load: {exc}",
                                               "type": "unavailable"}}, status_code=503)
            started = time.monotonic()
            matrix = model.embed(texts)
            compute_ms = (time.monotonic() - started) * 1000
        model_id = next(m for m, n in by_model.items() if n == name)
        data = [{"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(encode_vectors(matrix, encoding))]
        return JSONResponse(
            {"object": "list", "data": data, "model": model_id,
             "usage": {"prompt_tokens": 0, "total_tokens": 0}},
            headers={"x-harmony-unit": name, "x-compute-ms": f"{compute_ms:.1f}"})

    manager, coordinator = attach(
        app, host_id=host_id, kind="embed", units=units, idle_seconds=idle_seconds,
        coload=True, gpu_call=lambda fn: fn(), port=port,
        in_flight=busy, readiness=readiness, backend="cpu",
        # Warm after the bind (see serve._start_preload): the first query after
        # a restart should not pay the load.
        preload=list(units))
    holder["manager"] = manager
    app.state.embed_manager = manager
    app.state.embed_coordinator = coordinator

    def sweep() -> None:
        while True:
            time.sleep(SWEEP_INTERVAL_S)
            try:
                manager.maybe_evict()
            except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the node
                print(f"[embed] idle sweep failed: {exc}", flush=True)

    threading.Thread(target=sweep, name="embed-idle-sweep", daemon=True).start()
    return app


def main():
    import uvicorn
    port = int(os.environ.get("HARMONY_EMBED_PORT", "8220"))
    uvicorn.run(create_app(port=port), host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
