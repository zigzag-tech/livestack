"""PaddleOCR-VL spotting and recognition provider for ``jingway.perception.v1``."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable

from .contract import PerceptionContractError
from .locateanything import _materialized_image

_SPOT = re.compile(r"^\s*(.*?)((?:<\|LOC_\d+\|>){8})\s*$")
_LOC = re.compile(r"<\|LOC_(\d+)\|>")


def parse_spotting(raw: str, *, image: dict, max_items: int) -> tuple[list[dict], bool]:
    observations = []
    for line in raw.splitlines():
        match = _SPOT.match(line)
        if not match or not match.group(1).strip():
            continue
        values = [max(0, min(1000, int(value))) for value in _LOC.findall(match.group(2))]
        points = [{"x": values[i] * image["width"] / 1000,
                   "y": values[i + 1] * image["height"] / 1000} for i in range(0, 8, 2)]
        observations.append({
            "id": f"{image['id']}:text:{len(observations) + 1}", "imageId": image["id"],
            "text": match.group(1).strip(),
            "geometry": {"kind": "polygon", "polygon": {"exterior": points, "holes": []}},
            "attributes": {"reader": "paddleocr-vl-1.6"},
        })
        if len(observations) >= max_items:
            return observations, True
    return observations, False


class PaddleOcrVlAdapter:
    def __init__(self, *, model: str, revision: str, generate: Callable, device="cuda:0"):
        self.model, self.revision, self.generate, self.device = model, revision, generate, device

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        task = request["task"]; task_type = task["type"]
        if task_type not in ("text_detection", "text_recognition"):
            raise PerceptionContractError("unsupported", "PaddleOCR-VL serves text detection and recognition", 422)
        if task_type == "text_recognition" and task.get("regions"):
            raise PerceptionContractError("unsupported", "region recognition is not implemented", 422)
        max_items = int(request.get("limits", {}).get("maxItems") or 4096)
        max_tokens = int(request.get("limits", {}).get("maxOutputTokens") or 1024)
        prompt = "Spotting:" if task_type == "text_detection" else (task.get("prompt") or "OCR:")
        observations, raw_parts, truncated = [], [], False
        metrics = {"coldLoadMs": 0.0, "inferenceMs": 0.0, "totalMs": 0.0, "peakMemoryBytes": 0}
        for image in request["images"]:
            with _materialized_image(image) as path:
                raw, measured = self.generate(path, prompt, max_tokens, control)
            raw_parts.append(raw)
            truncated = truncated or bool(measured.get("truncated"))
            decoded = raw.replace("</s>", "").replace("<|im_end|>", "").strip()
            if task_type == "text_detection":
                rows, capped = parse_spotting(decoded, image=image, max_items=max(0, max_items-len(observations)))
                observations.extend(rows); truncated = truncated or capped
            else:
                observations.append({"id": f"ocr-{len(observations)+1}", "imageId": image["id"],
                                     "text": decoded[:16384], "attributes": {"reader": "paddleocr-vl-1.6"}})
                truncated = truncated or len(decoded) > 16384
            for source, target in (("cold_load_ms", "coldLoadMs"), ("inference_ms", "inferenceMs"), ("total_ms", "totalMs")):
                metrics[target] += float(measured.get(source, 0))
            metrics["peakMemoryBytes"] = max(metrics["peakMemoryBytes"], int(measured.get("peak_memory_bytes", 0)))
        joined = "\n\n".join(raw_parts)
        return {"schemaVersion":"jingway.perception.v1","requestId":request["requestId"],
                "outcome":"partial" if truncated else ("ok" if observations else "empty"),"observations":observations,"unprocessed":[],
                "truncated":truncated,"rawOutputSha256":hashlib.sha256(joined.encode()).hexdigest(),
                "execution":{"backend":"cuda","device":grant.get("device") or self.device,
                "implementation":"paddleocr-vl-transformers-v1","model":self.model,"modelRevision":self.revision,
                "precision":"bf16","preprocessingRevision":"paddleocr-vl-2x-lanczos-v1",
                "queueMs":float(grant.get("queue_ms",0)),**metrics}}


class PaddleOcrVlRuntime:
    def __init__(self, snapshot: str | Path): self.snapshot=str(snapshot); self._model=self._processor=None; self._load_ms=0.0
    def _load(self):
        if self._model is not None: return
        import torch
        if not torch.cuda.is_available(): raise PerceptionContractError("unavailable", "CUDA is required", 503)
        from transformers import AutoModelForImageTextToText, AutoProcessor
        started=time.perf_counter(); self._processor=AutoProcessor.from_pretrained(self.snapshot,local_files_only=True)
        self._model=AutoModelForImageTextToText.from_pretrained(self.snapshot,dtype=torch.bfloat16,local_files_only=True).to("cuda:0").eval(); self._load_ms=(time.perf_counter()-started)*1000
    def __call__(self,path:Path,prompt:str,max_tokens:int,control=None):
        self._load(); import torch
        from PIL import Image
        torch.cuda.reset_peak_memory_stats(); original=Image.open(path).convert("RGB"); image=original.resize((original.width*2,original.height*2),Image.Resampling.LANCZOS)
        messages=[{"role":"user","content":[{"type":"image","image":image},{"type":"text","text":prompt}]}]
        inputs=self._processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=True,return_dict=True,return_tensors="pt").to(self._model.device); started=time.perf_counter()
        from transformers import StoppingCriteria, StoppingCriteriaList
        class HarmonyStop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return bool(control and control.cancelled())
        stopping = StoppingCriteriaList([HarmonyStop()]) if control is not None else None
        with torch.inference_mode():
            output=self._model.generate(**inputs,max_new_tokens=max_tokens,do_sample=False,
                                        stopping_criteria=stopping)
        generated=output[0][inputs["input_ids"].shape[-1]:]; raw=self._processor.decode(generated,skip_special_tokens=False); infer=(time.perf_counter()-started)*1000
        result=(raw,{"cold_load_ms":self._load_ms,"inference_ms":infer,"total_ms":self._load_ms+infer,"peak_memory_bytes":int(torch.cuda.max_memory_allocated()),"truncated":int(generated.shape[-1])>=max_tokens}); self._load_ms=0.0; return result
    def close(self):
        self._model=self._processor=None; self._load_ms=0.0
        import gc,torch; gc.collect(); torch.cuda.empty_cache(); torch.cuda.ipc_collect()
