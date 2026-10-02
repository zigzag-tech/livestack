"""A reusable image worker advertising one model as a Harmony managed unit."""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Body, Header, HTTPException
from livestack_node import attach, counting
from livestack_node.client import admit
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .contract import satisfies, validate_request
from .runtime import ImageRuntime


def create_app(config: dict | None = None, runtime_factory=ImageRuntime):
    config = config or json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    token = Path(config["token_file"]).read_text().strip()
    attrs = {"class": "imagegen", "task": "text_to_image", "params_b": config["params_b"],
             "model": config["model"], "license": "apache-2.0", "quantization": config.get("quantization", "nf4")}
    unit_name = config["unit"]
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-gpu")
    holder, busy = {}, counting()
    request_lock = threading.Lock()

    def gpu_call(fn):
        return executor.submit(fn).result()

    def load():
        started = time.monotonic()
        runtime = runtime_factory(config["snapshot"], config["model"], steps=config["steps"], threads=config.get("cpu_threads", 8), quantization=config.get("quantization", "nf4"))
        holder["runtime"] = runtime
        holder["load_s"] = round(time.monotonic()-started, 3)
        return runtime

    def free():
        runtime = holder.pop("runtime", None)
        if runtime is not None:
            runtime.close()

    unit = ManagedUnit(unit_name, load, free, footprint=config["resident_bytes"],
                       residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
                       attributes=attrs, spread_group="imagegen", priority=30,
                       min_residency_s=60, reload_cost=120)
    app = FastAPI(title="Harmony image worker")
    manager, coordinator = attach(
        app, host_id=config["host_id"], kind="imagegen", units={unit_name: unit},
        idle_seconds=config.get("idle_seconds", 900), coload=True, gpu_call=gpu_call,
        port=config["port"], in_flight=busy, inventory=attrs,
        device_id=config["device_id"])

    @app.get("/health")
    def health():
        return {"ok": True, "model": config["model"], "loaded": unit.loaded}

    @app.post("/v1/images/generations")
    def generate(body: dict = Body(...), authorization: str | None = Header(None)):
        if not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "image worker credential required")
        try:
            request = validate_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not satisfies(attrs, request["harmony_requires"]):
            raise HTTPException(422, "worker does not satisfy the model requirements")
        max_dimension = config.get("max_dimension", 1024)
        if max(request["width"], request["height"]) > max_dimension:
            raise HTTPException(422, f"this worker is qualified for dimensions up to {max_dimension}px")
        # No lock on the GPU executor while admission may dispatch a warm to us.
        with request_lock, busy:
            started = time.monotonic()
            leased = coordinator.acquire_lease(unit_name, "harmony-image", ttl_seconds=3600)
            lease_id = leased.get("lease_id") if isinstance(leased, dict) else getattr(leased, "lease_id", None)
            try:
                grant = admit(unit_name, owner_id="harmony-image", brokers=[config["broker"]], timeout=1200)
                if not grant.get("granted") or grant.get("device_id") != config["device_id"]:
                    raise HTTPException(503, grant.get("reason") or "no concrete local Harmony grant")
                image, metrics = gpu_call(lambda: manager.run(unit_name, lambda runtime: runtime.generate(request)))
            finally:
                if lease_id:
                    coordinator.release_lease(lease_id)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}],
                    "harmony": {"model": config["model"], "revision": config["revision"],
                                "unit": unit_name, "host": config["host_id"], "device_id": config["device_id"],
                                "prompt": request["prompt"], "seed": request["seed"],
                                "width": request["width"], "height": request["height"],
                                "requirements": request["harmony_requires"], "local_grant": grant,
                                "total_s": round(time.monotonic()-started, 3), "load_s": holder.get("load_s"), **metrics}}
    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    uvicorn.run(create_app(config), host="0.0.0.0", port=config["port"])


if __name__ == "__main__":
    main()
