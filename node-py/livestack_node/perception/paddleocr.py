"""PP-OCR detector/reader provider for ``jingway.perception.v1``."""
from __future__ import annotations

import hashlib
import multiprocessing
import sys
import time
import traceback
from pathlib import Path

from .contract import PerceptionContractError
from .locateanything import _materialized_image


class PaddleOcrAdapter:
    def __init__(self, *, model: str, revision: str, run, device="cuda:0"):
        self.model, self.revision, self.run, self.device = model, revision, run, device

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        if request["task"]["type"] not in ("text_detection", "text_recognition"):
            raise PerceptionContractError("unsupported", "PP-OCR serves text detection and recognition", 422)
        if request["task"]["type"] == "text_recognition" and request["task"].get("regions"):
            raise PerceptionContractError("unsupported", "region recognition is not implemented", 422)
        maximum = int(request.get("limits", {}).get("maxItems") or 4096)
        observations, raw_rows, truncated = [], [], False
        metrics = {"coldLoadMs": 0.0, "inferenceMs": 0.0, "totalMs": 0.0,
                   "peakMemoryBytes": 0}
        for image in request["images"]:
            with _materialized_image(image) as path:
                rows, measured = self.run(path, control)
            raw_rows.extend(rows)
            remaining = max(0, maximum - len(observations))
            if len(rows) > remaining:
                rows, truncated = rows[:remaining], True
            for row in rows:
                observation = {
                    "id": f"{image['id']}:text:{len(observations)+1}",
                    "imageId": image["id"], "text": row["text"],
                    "geometry": {"kind": "polygon", "polygon": {
                        "exterior": [{"x": float(point[0]), "y": float(point[1])}
                                     for point in row["polygon"]], "holes": []}},
                    "attributes": {"reader": "pp-ocrv6-medium",
                                   "score": float(row["score"])},
                }
                observations.append(observation)
            for source, target in (("cold_load_ms", "coldLoadMs"),
                                   ("inference_ms", "inferenceMs"),
                                   ("total_ms", "totalMs")):
                metrics[target] += float(measured.get(source, 0))
            metrics["peakMemoryBytes"] = max(metrics["peakMemoryBytes"],
                                              int(measured.get("peak_memory_bytes", 0)))
        digest = hashlib.sha256(repr(raw_rows).encode()).hexdigest()
        return {
            "schemaVersion": "jingway.perception.v1", "requestId": request["requestId"],
            "outcome": "partial" if truncated else ("ok" if observations else "empty"),
            "observations": observations, "unprocessed": [], "truncated": truncated,
            "rawOutputSha256": digest,
            "execution": {"backend": "cuda", "device": grant.get("device") or self.device,
                          "implementation": "paddleocr-v6-medium-v1", "model": self.model,
                          "modelRevision": self.revision, "precision": "fp32",
                          "preprocessingRevision": "paddleocr-v6-native-v1",
                          "queueMs": float(grant.get("queue_ms", 0)), **metrics},
        }


def _worker(connection, detection_dir: str, recognition_dir: str) -> None:
    try:
        import numpy as np
        import paddle
        from PIL import Image
        from paddleocr import PaddleOCR
        if not paddle.is_compiled_with_cuda():
            raise RuntimeError("PaddlePaddle CUDA build is required")
        paddle.set_device("gpu:0")
        started = time.perf_counter()
        ocr = PaddleOCR(
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, enable_mkldnn=False,
            text_detection_model_dir=detection_dir,
            text_recognition_model_dir=recognition_dir)
        connection.send({"status": "ready", "cold_load_ms": (time.perf_counter()-started)*1000})
        while True:
            message = connection.recv()
            if message["op"] == "close":
                break
            if message["op"] != "infer":
                raise RuntimeError(f"unknown operation {message['op']!r}")
            started = time.perf_counter()
            payload = list(ocr.predict(np.asarray(Image.open(message["path"]).convert("RGB"))))[0].json["res"]
            rows = [{"text": text, "score": float(score),
                     "polygon": np.asarray(polygon).tolist()}
                    for text, score, polygon in zip(payload["rec_texts"], payload["rec_scores"],
                                                    payload["rec_polys"])]
            connection.send({"status": "ok", "rows": rows,
                             "inference_ms": (time.perf_counter()-started)*1000})
    except BaseException as exc:
        try:
            connection.send({"status": "error", "error": str(exc),
                             "traceback": traceback.format_exc()})
        except BaseException:
            pass
    finally:
        connection.close()


class PaddleOcrRuntime:
    def __init__(self, detection_dir: str | Path, recognition_dir: str | Path):
        self.detection_dir, self.recognition_dir = str(detection_dir), str(recognition_dir)
        self._process = self._connection = None
        self._load_ms = 0.0

    def _abort(self):
        connection, process = self._connection, self._process
        self._connection = self._process = None
        self._load_ms = 0.0
        if connection is not None:
            connection.close()
        if process is not None and process.is_alive():
            process.terminate(); process.join(timeout=2)

    def _load(self):
        if self._process is not None and self._process.is_alive():
            return
        for model_dir in (self.detection_dir, self.recognition_dir):
            if not Path(model_dir).is_dir():
                raise PerceptionContractError("unavailable", f"missing PP-OCR model directory: {model_dir}", 503)
        parent, child = multiprocessing.get_context("spawn").Pipe()
        process = multiprocessing.get_context("spawn").Process(
            target=_worker, args=(child, self.detection_dir, self.recognition_dir),
            name="harmony-paddleocr-v6", daemon=True)
        process.start(); child.close(); self._process, self._connection = process, parent
        if not self._connection.poll(120):
            self._abort(); raise PerceptionContractError("unavailable", "PP-OCR cold load timed out", 503, retryable=True)
        message = self._connection.recv()
        if message.get("status") != "ready":
            self._abort(); raise PerceptionContractError("unavailable", f"PP-OCR load failed: {message.get('error')}", 503)
        self._load_ms = float(message["cold_load_ms"])

    def __call__(self, path: Path, control=None):
        self._load()
        self._connection.send({"op": "infer", "path": str(path)})
        while not self._connection.poll(0.025):
            if control is not None and control.cancelled():
                self._abort()
                raise PerceptionContractError(control.cause or "cancelled", "PP-OCR inference cancelled", 408)
        message = self._connection.recv()
        if message.get("status") != "ok":
            raise PerceptionContractError("inference_failed", f"PP-OCR failed: {message.get('error')}", 500)
        inference_ms = float(message["inference_ms"])
        metrics = {"cold_load_ms": self._load_ms, "inference_ms": inference_ms,
                   "total_ms": self._load_ms + inference_ms, "peak_memory_bytes": 0}
        self._load_ms = 0.0
        return message["rows"], metrics

    def close(self):
        connection, process = self._connection, self._process
        self._connection = self._process = None; self._load_ms = 0.0
        if connection is not None:
            try: connection.send({"op": "close"})
            except (BrokenPipeError, EOFError, OSError): pass
            connection.close()
        if process is not None:
            process.join(timeout=5)
            if process.is_alive(): process.terminate(); process.join(timeout=2)
