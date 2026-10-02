"""ONNX sentence embedding on the CPU: tokenizer -> encoder -> mean pool -> L2.

This is the computation transformers.js's ``feature-extraction`` pipeline does
with ``{pooling: "mean", normalize: true}``, for the same ONNX files, so a
vector from here is comparable with one a hub produced in-process from the
same weights. Comparable, not identical — a different runtime build may differ
in the last bits — which is why a caller tags stored vectors with the model AND
its quantization and re-embeds on a change of either.

Dependencies are the minimum that can run it: ``onnxruntime``, ``tokenizers``
and ``numpy``. No torch: a CPU node must not drag a CUDA runtime onto a build
host to embed a sentence.
"""
from __future__ import annotations

import json
import os
from typing import Callable, List, Sequence


class EmbeddingCancelled(Exception):
    """The caller stopped this batch; no subsequent inference slice may run."""

# Files a model directory must hold. The ONNX file is the q8 export
# (`onnx/model_quantized.onnx`) by default — the fp32 file is 4x the bytes and
# RAM for no ranking difference anyone measured here.
ONNX_FILES = {"q8": "onnx/model_quantized.onnx", "fp32": "onnx/model.onnx"}

# ONE TEXT PER INFERENCE CALL. Two measured reasons, either sufficient:
#
# * A q8 export quantizes activations DYNAMICALLY — one scale per tensor, padded
#   positions included — so a text's vector depended on what it was batched
#   with: 0.0185 max-abs difference for the same text alone vs in a batch of
#   three (zz-joe, 2026-10-01). A cached vector must depend only on its text.
# * Padding is waste on a CPU: 150 mixed-length docs took 3.05 s one at a time
#   and 6.76 s in batches of 8 (multilingual MiniLM, 4 threads). It also bounds
#   peak memory by one input, not by a caller's batch — the attention spike that
#   got the in-process embedder OOM-killed on the public hub.
INFERENCE_SLICE = 1

# The longest input a position table of 512 can encode, whatever a tokenizer
# config claims (some say 1e30 for "unbounded").
MAX_POSITIONS = 512


def model_files(model_dir: str, quant: str) -> dict:
    """The paths a load needs, or ValueError naming the first missing one."""
    if quant not in ONNX_FILES:
        raise ValueError(f"unknown quantization {quant!r}; known: {sorted(ONNX_FILES)}")
    out = {
        "onnx": os.path.join(model_dir, ONNX_FILES[quant]),
        "tokenizer": os.path.join(model_dir, "tokenizer.json"),
        "tokenizer_config": os.path.join(model_dir, "tokenizer_config.json"),
    }
    for name, path in out.items():
        if not os.path.isfile(path):
            raise ValueError(f"model directory {model_dir} has no {name} file ({path})")
    return out


class OnnxSentenceEmbedder:
    """One loaded model. ``embed(texts)`` -> float32 array [n, dim], rows unit length.

    Thread-safe: an ONNX Runtime session may be run concurrently, and the
    tokenizer's padding/truncation are fixed at load, so ``encode_batch`` only
    reads shared state.
    """

    def __init__(self, model_dir: str, *, quant: str = "q8", threads: int = 2):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        files = model_files(model_dir, quant)
        with open(files["tokenizer_config"]) as fh:
            config = json.load(fh)
        max_len = int(min(float(config.get("model_max_length") or MAX_POSITIONS), MAX_POSITIONS))
        tok = Tokenizer.from_file(files["tokenizer"])
        tok.enable_truncation(max_length=max_len)
        pad_token = config.get("pad_token") or "[PAD]"
        if isinstance(pad_token, dict):
            pad_token = pad_token.get("content") or "[PAD]"
        pad_id = tok.token_to_id(pad_token)
        tok.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token=pad_token)

        opts = ort.SessionOptions()
        # Bounded explicitly: ONNX defaults to one thread per core, which on a
        # shared build host competes with every compile for no latency gain.
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        # The CPU arena never returns memory to the OS, so one peak slice would
        # become this process's permanent floor.
        opts.enable_cpu_mem_arena = False
        self._session = ort.InferenceSession(files["onnx"], sess_options=opts,
                                             providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tok = tok
        self._np = np
        self.max_len = max_len
        self.dim = int(self._session.get_outputs()[0].shape[-1])

    def embed(self, texts: Sequence[str], *, cancelled: Callable[[], bool] = lambda: False) -> "object":
        np = self._np
        out: List[object] = []
        for start in range(0, len(texts), INFERENCE_SLICE):
            if cancelled():
                raise EmbeddingCancelled("embedding request cancelled")
            # An empty string tokenizes to nothing but specials on some
            # tokenizers and to an error on some endpoints; a space is what the
            # hub has always sent in its place.
            chunk = [t if t else " " for t in texts[start:start + INFERENCE_SLICE]]
            enc = self._tok.encode_batch(chunk)
            ids = np.asarray([e.ids for e in enc], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            if cancelled():
                raise EmbeddingCancelled("embedding request cancelled")
            hidden = self._session.run(None, feed)[0]            # [b, seq, dim]
            if cancelled():
                raise EmbeddingCancelled("embedding request cancelled")
            m = mask[:, :, None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            out.append((pooled / np.clip(norms, 1e-12, None)).astype(np.float32))
        if cancelled():
            raise EmbeddingCancelled("embedding request cancelled")
        if not out:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.concatenate(out, axis=0)
