"""Managed Harmony worker for PP-OCRv6 medium."""
from __future__ import annotations

import os
import threading

from livestack_node import attach, counting
from livestack_node.fleet_auth import principals_from_env
from livestack_node.manager import ManagedUnit, ResidencyPolicy

from .admission import PADDLEOCR_V6_UNIT, broker_admission_payload
from .contract import PerceptionContractError
from .paddleocr import PaddleOcrAdapter, PaddleOcrRuntime
from .serve import _post_json
from .service import PerceptionService, build_app


def create_app():
    port = int(os.environ.get("HARMONY_PERCEPTION_PORT", "8206"))
    broker = os.environ.get("HARMONY_PERCEPTION_BROKER", "http://127.0.0.1:8799").rstrip("/")
    broker_token = os.environ.get("HARMONY_PERCEPTION_BROKER_TOKEN") or None
    detection = os.environ["PADDLEOCR_DETECTION_DIR"]
    recognition = os.environ["PADDLEOCR_RECOGNITION_DIR"]
    model = os.environ.get("PADDLEOCR_MODEL", "PaddlePaddle/PP-OCRv6-medium")
    revision = os.environ.get("PADDLEOCR_REVISION", "local-official-models")
    busy, lock, holder = counting(), threading.Lock(), {}

    def load_runtime():
        runtime = PaddleOcrRuntime(detection, recognition); runtime._load(); holder["runtime"] = runtime; return runtime
    def free_runtime():
        runtime = holder.pop("runtime", None)
        if runtime is not None: runtime.close()
    units = {PADDLEOCR_V6_UNIT: ManagedUnit(
        PADDLEOCR_V6_UNIT, load_runtime, free_runtime,
        footprint=int(os.environ.get("PADDLEOCR_RESIDENT_BYTES", str(3*1024**3))),
        residency_policy=ResidencyPolicy.UNPINNED, min_resident=0,
        spread_group="ocr-reader", attributes={"class":"perception",
        "task":"text_detection,text_recognition", "backend":"cuda", "model":model},
        reload_cost=15.0)}
    principals = principals_from_env(file_var="HARMONY_PERCEPTION_TOKENS_FILE",
        inline_var="HARMONY_PERCEPTION_TOKENS", log=lambda message: print(message, flush=True))
    if not principals: raise RuntimeError("Harmony perception ingress requires a configured principal")
    def admit(*, request, owner, realm):
        required=request.get("requirements",{})
        if required.get("model") not in (None,model,PADDLEOCR_V6_UNIT):
            raise PerceptionContractError("unsupported","requested model is not served",422)
        if required.get("backend") not in (None,"cuda"):
            raise PerceptionContractError("unsupported","PP-OCR requires CUDA",422)
        if request["task"]["type"] not in ("text_detection","text_recognition"):
            raise PerceptionContractError("unsupported","worker serves OCR",422)
        result=_post_json(f"{broker}/admit",broker_admission_payload(request["requestId"],owner,PADDLEOCR_V6_UNIT),broker_token)
        if not result.get("granted"):
            raise PerceptionContractError("unavailable",result.get("reason","Harmony refused capacity"),503,retryable=True)
        return {"backend":"cuda","device":result.get("device_id") or "cuda:0","queue_ms":0,"lease_id":result.get("lease_id")}
    def run(path,control=None):
        with busy,lock:return holder["manager"].run(PADDLEOCR_V6_UNIT,lambda runtime:runtime(path,control))
    app=build_app(PerceptionService(principals=principals,backends={"cuda":PaddleOcrAdapter(model=model,revision=revision,run=run)},admit=admit))
    manager,coordinator=attach(app,host_id=os.environ.get("HARMONY_PERCEPTION_HOST_ID","xc-tower-paddleocr-v6"),kind="perception",units=units,idle_seconds=int(os.environ.get("HARMONY_PERCEPTION_IDLE_SECONDS","900")),coload=True,gpu_call=lambda fn:fn(),port=port,in_flight=busy,device_id=os.environ.get("HARMONY_PERCEPTION_DEVICE_ID") or None)
    holder["manager"]=manager;app.state.perception_manager=manager;app.state.perception_coordinator=coordinator;return app


def main():
    import uvicorn
    uvicorn.run(create_app(),host="0.0.0.0",port=int(os.environ.get("HARMONY_PERCEPTION_PORT","8206")),workers=1)


if __name__ == "__main__": main()
