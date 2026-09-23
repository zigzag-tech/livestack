"""Run a Harmony perception node with persistent, admitted model adapters."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from livestack_node import attach, counting
from livestack_node.fleet_auth import principals_from_env
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .admission import (LOCATEANYTHING_CUDA_UNIT, LOCATEANYTHING_MLX_UNIT,
                        broker_admission_payload)
from .contract import PerceptionContractError
from .locateanything import (CudaLocateAnythingRuntime, LocateAnythingAdapter,
                             MlxLocateAnythingRuntime)
from .remote import (RemotePerceptionAdapter, load_remote_routes,
                     matching_route, read_token)
from .service import PerceptionService, build_app

def _post_json(url: str, payload: dict, token: str | None = None, timeout: float = 600) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PerceptionContractError("unavailable", f"Harmony admission failed: {exc}", 503, retryable=True) from exc


def create_app():
    host_id = os.environ.get("HARMONY_PERCEPTION_HOST_ID", "xc-tower-ubuntu-perception")
    port = int(os.environ.get("HARMONY_PERCEPTION_PORT", "8200"))
    broker = os.environ.get("HARMONY_PERCEPTION_BROKER", "http://127.0.0.1:8799").rstrip("/")
    broker_token = os.environ.get("HARMONY_PERCEPTION_BROKER_TOKEN") or None
    backend_name = os.environ.get("HARMONY_PERCEPTION_BACKEND", "cuda").strip().lower()
    if backend_name not in ("cuda", "mlx"):
        raise RuntimeError("HARMONY_PERCEPTION_BACKEND must be cuda or mlx")
    unit = LOCATEANYTHING_CUDA_UNIT if backend_name == "cuda" else LOCATEANYTHING_MLX_UNIT
    snapshot = os.environ.get("LOCATEANYTHING_SNAPSHOT")
    default_revision = ("c32291ca5e996f5a7a485845b4f57a233936bba0" if backend_name == "cuda"
                        else "main")
    model_revision = os.environ.get("LOCATEANYTHING_REVISION", default_revision)
    model_id = ("nvidia/LocateAnything-3B" if backend_name == "cuda"
                else os.environ.get("LOCATEANYTHING_MODEL", "mlx-community/LocateAnything-3B-8bit"))
    footprint = int(os.environ.get("LOCATEANYTHING_RESIDENT_BYTES", str(8 * 1024**3)))
    busy = counting()
    gpu_lock = threading.Lock()
    holder: dict[str, object] = {}
    remote_routes = load_remote_routes(os.environ.get("HARMONY_PERCEPTION_REMOTE_ROUTES_FILE"))
    # Metal model lifecycle calls must remain on one owned thread. Running MLX
    # initialization in an arbitrary AnyIO request worker can block inside the
    # provider loader; the same executor also makes warm/infer/evict ordering
    # explicit. CUDA keeps its existing direct execution path.
    mlx_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harmony-mlx") \
        if backend_name == "mlx" else None

    def accelerator_call(fn):
        return mlx_executor.submit(fn).result() if mlx_executor else fn()

    def load_runtime():
        runtime = (CudaLocateAnythingRuntime(snapshot) if backend_name == "cuda"
                   else MlxLocateAnythingRuntime(snapshot or model_id,
                                                  None if snapshot else model_revision))
        runtime._load()
        holder["runtime"] = runtime
        return runtime

    def free_cuda():
        runtime = holder.pop("runtime", None)
        if runtime is not None:
            runtime.close()
        import gc
        gc.collect()
        if backend_name == "cuda":
            import torch
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()

    units = {unit: ManagedUnit(
        unit, load_runtime, free_cuda, footprint=footprint,
        residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
        spread_group="grounding", attributes={
            "class": "perception", "task": "grounding", "backend": backend_name,
            "model": model_id,
        }, reload_cost=35.0,
    )}

    principals = principals_from_env(
        file_var="HARMONY_PERCEPTION_TOKENS_FILE",
        inline_var="HARMONY_PERCEPTION_TOKENS",
        log=lambda message: print(message, flush=True),
    )
    if not principals:
        raise RuntimeError("Harmony perception ingress requires at least one configured principal")

    def admit(*, request, owner, realm):
        required = request.get("requirements", {})
        remote = matching_route(remote_routes, required)
        if remote is not None:
            return {"backend": f"remote:{remote['name']}", "device": "harmony-federated",
                    "queue_ms": 0, "owner": owner, "realm": realm}
        wanted_model = required.get("model")
        if wanted_model not in (None, model_id, unit):
            raise PerceptionContractError("unsupported", f"model {wanted_model!r} is not served by this node", 422)
        if required.get("backend") not in (None, backend_name):
            raise PerceptionContractError("unsupported", f"this node serves the {backend_name} backend", 422)
        result = _post_json(
            f"{broker}/admit", broker_admission_payload(request["requestId"], owner, unit), broker_token,
        )
        if not result.get("granted"):
            raise PerceptionContractError("unavailable", result.get("reason", "Harmony refused capacity"), 503, retryable=True)
        return {"backend": backend_name,
                "device": result.get("device_id") or ("cuda:0" if backend_name == "cuda" else "mlx0"),
                "queue_ms": 0,
                "lease_id": result.get("lease_id")}

    def generate(path, prompt, max_tokens, control=None):
        manager = holder["manager"]
        with busy, gpu_lock:
            return accelerator_call(
                lambda: manager.run(unit, lambda runtime: runtime(path, prompt, max_tokens, control)))

    adapter = LocateAnythingAdapter(
        backend=backend_name, device="cuda:0" if backend_name == "cuda" else "mlx0",
        implementation=f"locateanything-{backend_name}-v1",
        model=model_id, model_revision=model_revision,
        precision="bf16" if backend_name == "cuda" else "int8",
        preprocessing_revision=f"locateanything-{backend_name}-{model_revision}", generate=generate,
    )
    backends = {backend_name: adapter}
    for route in remote_routes:
        backends[f"remote:{route['name']}"] = RemotePerceptionAdapter(
            url=route["url"], token=read_token(route["tokenFile"]),
            timeout=float(route.get("timeoutSeconds", 900)))
    service = PerceptionService(principals=principals, backends=backends, admit=admit)
    app = build_app(service)

    manager, coordinator = attach(
        app, host_id=host_id, kind="perception", units=units,
        idle_seconds=int(os.environ.get("HARMONY_PERCEPTION_IDLE_SECONDS", "900")),
        coload=True, gpu_call=accelerator_call, port=port, in_flight=busy,
        device_id=os.environ.get("HARMONY_PERCEPTION_DEVICE_ID") or None,
    )
    holder["manager"] = manager
    app.state.perception_manager = manager
    app.state.perception_coordinator = coordinator
    return app


def main():
    import uvicorn
    port = int(os.environ.get("HARMONY_PERCEPTION_PORT", "8200"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
