"""Managed Harmony worker for SAM2.1 small segmentation."""
from __future__ import annotations

import os
import threading

from livestack_node import attach, counting
from livestack_node.fleet_auth import principals_from_env
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .admission import SAM21_UNIT, broker_admission_payload
from .contract import PerceptionContractError
from .sam21 import Sam21Adapter, Sam21Runtime
from .serve import _post_json
from .service import PerceptionService, build_app


def create_app():
    host_id = os.environ.get("HARMONY_PERCEPTION_HOST_ID", "xc-tower-sam21")
    port = int(os.environ.get("HARMONY_PERCEPTION_PORT", "8205"))
    broker = os.environ.get("HARMONY_PERCEPTION_BROKER", "http://127.0.0.1:8799").rstrip("/")
    broker_token = os.environ.get("HARMONY_PERCEPTION_BROKER_TOKEN") or None
    checkpoint = os.environ.get("SAM21_CHECKPOINT")
    if not checkpoint:
        raise RuntimeError("SAM21_CHECKPOINT is required")
    model_id = os.environ.get("SAM21_MODEL", "facebook/sam2.1-hiera-small")
    revision = os.environ.get("SAM21_REVISION", "sam2.1_s.pt")
    footprint = int(os.environ.get("SAM21_RESIDENT_BYTES", str(3 * 1024**3)))
    busy, lock, holder = counting(), threading.Lock(), {}

    def load_runtime():
        runtime = Sam21Runtime(checkpoint); runtime._load(); holder["runtime"] = runtime; return runtime

    def free_runtime():
        runtime = holder.pop("runtime", None)
        if runtime is not None: runtime.close()

    units = {SAM21_UNIT: ManagedUnit(
        SAM21_UNIT, load_runtime, free_runtime, footprint=footprint,
        residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
        spread_group="segmentation", attributes={"class": "perception", "task": "segmentation",
                                                  "backend": "cuda", "model": model_id}, reload_cost=10.0)}
    principals = principals_from_env(file_var="HARMONY_PERCEPTION_TOKENS_FILE",
                                     inline_var="HARMONY_PERCEPTION_TOKENS", log=lambda m: print(m, flush=True))
    if not principals: raise RuntimeError("Harmony perception ingress requires a configured principal")

    def admit(*, request, owner, realm):
        required = request.get("requirements", {})
        if required.get("model") not in (None, model_id, SAM21_UNIT):
            raise PerceptionContractError("unsupported", "requested model is not served", 422)
        if required.get("backend") not in (None, "cuda"):
            raise PerceptionContractError("unsupported", "SAM2.1 requires CUDA", 422)
        if request["task"]["type"] != "segmentation":
            raise PerceptionContractError("unsupported", "worker serves segmentation", 422)
        result = _post_json(f"{broker}/admit", broker_admission_payload(request["requestId"], owner, SAM21_UNIT), broker_token)
        if not result.get("granted"):
            raise PerceptionContractError("unavailable", result.get("reason", "Harmony refused capacity"), 503, retryable=True)
        return {"backend": "cuda", "device": result.get("device_id") or "cuda:0", "queue_ms": 0,
                "lease_id": result.get("lease_id")}

    def segment(path, groups):
        with busy, lock:
            return holder["manager"].run(SAM21_UNIT, lambda runtime: runtime(path, groups))

    adapter = Sam21Adapter(model=model_id, revision=revision, segment=segment)
    app = build_app(PerceptionService(principals=principals, backends={"cuda": adapter}, admit=admit))
    manager, coordinator = attach(app, host_id=host_id, kind="perception", units=units,
                                  idle_seconds=int(os.environ.get("HARMONY_PERCEPTION_IDLE_SECONDS", "900")),
                                  coload=True, gpu_call=lambda fn: fn(), port=port, in_flight=busy,
                                  device_id=os.environ.get("HARMONY_PERCEPTION_DEVICE_ID") or None)
    holder["manager"] = manager
    app.state.perception_manager = manager; app.state.perception_coordinator = coordinator
    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("HARMONY_PERCEPTION_PORT", "8205")), workers=1)


if __name__ == "__main__": main()
