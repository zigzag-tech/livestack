"""SAM2.1 interactive segmentation provider for ``jingway.perception.v1``."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from .contract import PerceptionContractError
from .locateanything import _materialized_image


def _contours(mask, *, epsilon: float = 1.0) -> list[dict]:
    import cv2
    import numpy as np

    binary = np.asarray(mask, dtype=np.uint8)
    found, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]

    def points(index: int) -> list[dict]:
        contour = cv2.approxPolyDP(found[index], epsilon, True).reshape(-1, 2)
        return [{"x": float(x), "y": float(y)} for x, y in contour]

    polygons = []
    for index, relation in enumerate(hierarchy):
        if relation[3] != -1:
            continue
        exterior = points(index)
        if len(exterior) < 3:
            continue
        holes = []
        child = relation[2]
        while child != -1:
            ring = points(child)
            if len(ring) >= 3:
                holes.append(ring)
            child = hierarchy[child][0]
        polygons.append({"exterior": exterior, "holes": holes})
    return polygons


def _bounds(polygon: dict) -> dict:
    xs = [point["x"] for point in polygon["exterior"]]
    ys = [point["y"] for point in polygon["exterior"]]
    return {"xMin": min(xs), "yMin": min(ys), "xMax": max(xs), "yMax": max(ys)}


class Sam21Adapter:
    def __init__(self, *, model: str, revision: str, segment: Callable, device="cuda:0"):
        self.model, self.revision, self.segment, self.device = model, revision, segment, device

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        task = request["task"]
        if task["type"] != "segmentation":
            raise PerceptionContractError("unsupported", "SAM2.1 serves segmentation", 422)
        unsupported = sorted(set(task["return"]) - {"polygon", "box"})
        if unsupported:
            raise PerceptionContractError(
                "unsupported", "SAM2.1 worker currently returns polygon and box geometry; raster mask transport is unavailable", 422)
        prompts = task["prompts"]
        if any(prompt["type"] in {"text", "exemplar"} for prompt in prompts):
            raise PerceptionContractError("unsupported", "SAM2.1 supports point and box prompts", 422)
        boxes = [prompt for prompt in prompts if prompt["type"] == "box"]
        points = [prompt for prompt in prompts if prompt["type"] == "point"]
        if boxes and points:
            raise PerceptionContractError("invalid_input", "use either box prompts or one positive/negative point group", 422)
        if points and not any(prompt["label"] == "positive" for prompt in points):
            raise PerceptionContractError("invalid_input", "a point group requires at least one positive point", 422)

        observations, raw_hashes = [], []
        truncated = False
        metrics = {"coldLoadMs": 0.0, "inferenceMs": 0.0, "totalMs": 0.0, "peakMemoryBytes": 0}
        max_items = int(request.get("limits", {}).get("maxItems") or 4096)
        for image in request["images"]:
            groups = []
            if boxes:
                groups = [{"query_id": prompt["id"], "box": prompt["box"], "points": []} for prompt in boxes]
            else:
                groups = [{"query_id": next(prompt["id"] for prompt in points if prompt["label"] == "positive"),
                           "box": None, "points": points}]
            with _materialized_image(image) as path:
                masks, measured = self.segment(path, groups)
            for key, target in (("cold_load_ms", "coldLoadMs"), ("inference_ms", "inferenceMs"), ("total_ms", "totalMs")):
                metrics[target] += float(measured.get(key, 0))
            metrics["peakMemoryBytes"] = max(metrics["peakMemoryBytes"], int(measured.get("peak_memory_bytes", 0)))
            for group, mask in zip(groups, masks):
                mask_bytes = memoryview(mask).tobytes()
                raw_hashes.append(hashlib.sha256(mask_bytes).hexdigest())
                polygons = _contours(mask)
                for component, polygon in enumerate(polygons, start=1):
                    attributes = {"segmenter": "sam2.1-small", "component": component,
                                  "promptIds": [p["id"] for p in group["points"]] or [group["query_id"]]}
                    for kind in task["return"]:
                        if len(observations) >= max_items:
                            truncated = True
                            break
                        geometry = {"kind": "polygon", "polygon": polygon} if kind == "polygon" else {"kind": "box", "box": _bounds(polygon)}
                        observations.append({"id": f"{image['id']}:{group['query_id']}:{component}:{kind}",
                                             "imageId": image["id"], "queryId": group["query_id"],
                                             "geometry": geometry, "attributes": attributes})
                    if truncated:
                        break
        digest = hashlib.sha256("\n".join(raw_hashes).encode()).hexdigest()
        return {"schemaVersion": "jingway.perception.v1", "requestId": request["requestId"],
                "outcome": "partial" if truncated else ("ok" if observations else "empty"),
                "observations": observations, "unprocessed": [], "truncated": truncated,
                "rawOutputSha256": digest,
                "execution": {"backend": "cuda", "device": grant.get("device") or self.device,
                              "implementation": "ultralytics-sam21-v1", "model": self.model,
                              "modelRevision": self.revision, "precision": "fp32",
                              "preprocessingRevision": "ultralytics-8.4-source-pixels-v1",
                              "queueMs": float(grant.get("queue_ms", 0)), **metrics}}


class Sam21Runtime:
    def __init__(self, checkpoint: str | Path):
        self.checkpoint = str(checkpoint)
        self._model = None
        self._load_ms = 0.0

    def _load(self):
        if self._model is not None:
            return
        import torch
        if not torch.cuda.is_available():
            raise PerceptionContractError("unavailable", "CUDA is required", 503)
        from ultralytics import SAM
        started = time.perf_counter()
        self._model = SAM(self.checkpoint)
        self._load_ms = (time.perf_counter() - started) * 1000

    def __call__(self, path: Path, groups: list[dict]):
        import torch
        self._load()
        torch.cuda.reset_peak_memory_stats()
        masks = []
        started = time.perf_counter()
        for group in groups:
            kwargs = {"device": 0, "verbose": False}
            if group["box"] is not None:
                box = group["box"]
                kwargs["bboxes"] = [[box["xMin"], box["yMin"], box["xMax"], box["yMax"]]]
            else:
                kwargs["points"] = [[[p["point"]["x"], p["point"]["y"]] for p in group["points"]]]
                kwargs["labels"] = [[1 if p["label"] == "positive" else 0 for p in group["points"]]]
            result = self._model.predict(str(path), **kwargs)[0]
            if result.masks is None or len(result.masks.data) == 0:
                import numpy as np
                masks.append(np.zeros((result.orig_shape[0], result.orig_shape[1]), dtype=bool))
            else:
                masks.append(result.masks.data[0].detach().cpu().numpy().astype(bool))
        inference_ms = (time.perf_counter() - started) * 1000
        measured = {"cold_load_ms": self._load_ms, "inference_ms": inference_ms,
                    "total_ms": self._load_ms + inference_ms,
                    "peak_memory_bytes": int(torch.cuda.max_memory_allocated())}
        self._load_ms = 0.0
        return masks, measured

    def close(self):
        self._model = None
        self._load_ms = 0.0
        import gc
        import torch
        gc.collect(); torch.cuda.empty_cache(); torch.cuda.ipc_collect()
