"""DeepSeek-OCR-2 provider for ``jingway.perception.v1`` text recognition."""
from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path
from typing import Callable

from .contract import PerceptionContractError
from .locateanything import _materialized_image


class DeepSeekOCR2Adapter:
    def __init__(self, *, model: str, revision: str, generate: Callable,
                 device: str = "cuda:0"):
        self.model = model
        self.revision = revision
        self.generate = generate
        self.device = device

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        task = request["task"]
        if task["type"] != "text_recognition":
            raise PerceptionContractError(
                "unsupported", "DeepSeek-OCR-2 serves text_recognition", 422)
        regions = task.get("regions") or []
        prompt = task.get("prompt") or "<image>\nFree OCR. "
        max_tokens = int(request.get("limits", {}).get("maxOutputTokens") or 1024)
        max_items = int(request.get("limits", {}).get("maxItems") or 4096)
        observations, unprocessed, raw_parts = [], [], []
        total_metrics = {"coldLoadMs": 0.0, "inferenceMs": 0.0,
                         "totalMs": 0.0, "peakMemoryBytes": 0}
        truncated = False

        work = []
        for image in request["images"]:
            if regions:
                work.extend((image, region) for region in regions)
            else:
                work.append((image, None))
        if len(work) > max_items:
            for image, region in work[max_items:]:
                unprocessed.append({"id": region["id"] if region else image["id"],
                                    "cause": "maxItems limit"})
            work = work[:max_items]
            truncated = True

        for image, region in work:
            with _materialized_image(image) as source:
                inference_path = source
                crop_path = None
                try:
                    if region is not None:
                        from PIL import Image
                        points = region["polygon"]["exterior"]
                        left, top = min(p["x"] for p in points), min(p["y"] for p in points)
                        right, bottom = max(p["x"] for p in points), max(p["y"] for p in points)
                        if right <= left or bottom <= top:
                            raise PerceptionContractError("invalid_input", "empty OCR region", 422)
                        handle = tempfile.NamedTemporaryFile(
                            prefix="harmony-ocr-region-", suffix=".png", delete=False)
                        crop_path = Path(handle.name)
                        handle.close()
                        with Image.open(source) as opened:
                            opened.crop((left, top, right, bottom)).save(crop_path)
                        inference_path = crop_path
                    raw, metrics = self.generate(inference_path, prompt, max_tokens, control)
                finally:
                    if crop_path is not None:
                        crop_path.unlink(missing_ok=True)
            raw_parts.append(raw)
            text = raw[:16_384]
            truncated = truncated or len(raw) > len(text) or bool(metrics.get("truncated"))
            observation = {
                "id": f"ocr-{len(observations) + 1}", "imageId": image["id"],
                "text": text, "attributes": {"reader": "deepseek-ocr-2"},
            }
            if region is not None:
                observation["queryId"] = region["id"]
                observation["geometry"] = {"kind": "polygon", "polygon": region["polygon"]}
            observations.append(observation)
            for source, target in (("cold_load_ms", "coldLoadMs"),
                                   ("inference_ms", "inferenceMs"),
                                   ("total_ms", "totalMs")):
                total_metrics[target] += float(metrics.get(source, 0))
            total_metrics["peakMemoryBytes"] = max(
                total_metrics["peakMemoryBytes"], int(metrics.get("peak_memory_bytes", 0)))

        joined = "\n\n".join(raw_parts)
        return {
            "schemaVersion": "jingway.perception.v1", "requestId": request["requestId"],
            "outcome": "partial" if truncated else ("ok" if observations else "empty"), "observations": observations,
            "unprocessed": unprocessed, "truncated": truncated,
            "rawOutputSha256": hashlib.sha256(joined.encode()).hexdigest(),
            "execution": {
                "backend": "cuda", "device": grant.get("device") or self.device,
                "implementation": "deepseek-ocr2-transformers-v1", "model": self.model,
                "modelRevision": self.revision, "precision": "bf16",
                "preprocessingRevision": "deepseek-ocr2-official-dynamic-v1",
                "queueMs": float(grant.get("queue_ms", 0)), **total_metrics,
            },
        }


class DeepSeekOCR2Runtime:
    def __init__(self, snapshot: str | Path, *, attention: str = "flash_attention_2"):
        self.snapshot = str(snapshot)
        self.attention = attention
        self._model = self._tokenizer = None
        self._load_ms = 0.0

    def _load(self):
        if self._model is not None:
            return
        import torch
        if not torch.cuda.is_available():
            raise PerceptionContractError("unavailable", "CUDA is required", 503)
        from transformers import AutoModel, AutoTokenizer
        started = time.perf_counter()
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.snapshot, trust_remote_code=True, local_files_only=True)
        self._model = AutoModel.from_pretrained(
            self.snapshot, _attn_implementation=self.attention,
            trust_remote_code=True, use_safetensors=True, local_files_only=True)
        self._model = self._model.eval().cuda().to(torch.bfloat16)
        self._load_ms = (time.perf_counter() - started) * 1000

    def __call__(self, path: Path, prompt: str, max_tokens: int, control=None) -> tuple[str, dict]:
        self._load()
        import torch
        torch.cuda.reset_peak_memory_stats()
        original_generate = self._model.generate

        def bounded_generate(*args, **kwargs):
            from transformers import StoppingCriteria, StoppingCriteriaList
            class HarmonyStop(StoppingCriteria):
                def __call__(self, input_ids, scores, **inner_kwargs):
                    return bool(control and control.cancelled())
            kwargs["max_new_tokens"] = min(int(kwargs.get("max_new_tokens", max_tokens)),
                                            max_tokens)
            existing = list(kwargs.get("stopping_criteria") or [])
            kwargs["stopping_criteria"] = StoppingCriteriaList(existing + [HarmonyStop()])
            return original_generate(*args, **kwargs)

        self._model.generate = bounded_generate
        started = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="harmony-deepseek-ocr2-") as output:
                raw = self._model.infer(
                    self._tokenizer, prompt=prompt, image_file=str(path), output_path=output,
                    base_size=1024, image_size=768, crop_mode=True,
                    save_results=False, eval_mode=True)
        finally:
            self._model.generate = original_generate
        inference_ms = (time.perf_counter() - started) * 1000
        metrics = {
            "cold_load_ms": self._load_ms, "inference_ms": inference_ms,
            "total_ms": self._load_ms + inference_ms,
            "peak_memory_bytes": int(torch.cuda.max_memory_allocated()),
            "truncated": False,
        }
        self._load_ms = 0.0
        return str(raw), metrics

    def close(self):
        self._model = self._tokenizer = None
        self._load_ms = 0.0
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
