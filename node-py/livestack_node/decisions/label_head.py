"""A trained label head: Simple Jev's readout corrected by the served model's hidden state.

A model that cannot carry a decision adapter (Flash-Next on Strata loads no LoRA)
can still have its readout trained. Strata's `strata_hidden` returns the vector
the head multiplies at the label position; a head file adds a learned term to
each candidate label's logprob:

    z_j = logprob_j + ((h - mu) / sd) . U[:, j] + c[j]

trained by scripts/flash_label_head.py. The file is an .npz with float arrays
U (dim x labels), c (labels), mu and sd (dim), and `meta`, a JSON string naming
the served model it was trained on ("served_model", the reply's `model`) and
the vector size ("dim"). A head is only applied to the model it was trained on:
anything else is refused, never scored with a head that does not fit.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Mapping

_CACHE: dict = {}


class HeadError(ValueError):
    pass


def load(path: str) -> dict:
    """The head at `path`, cached until the file changes."""
    import numpy as np
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError as exc:
        raise HeadError(f"decision head file {path!r} cannot be read") from exc
    hit = _CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    with np.load(path, allow_pickle=False) as f:
        head = {k: f[k] for k in ("U", "c", "mu", "sd")}
        meta = json.loads(str(f["meta"]))
    dim = int(meta["dim"])
    if head["U"].shape[0] != dim or head["mu"].shape != (dim,) or head["sd"].shape != (dim,) \
            or head["c"].shape != (head["U"].shape[1],):
        raise HeadError(f"decision head {path!r} has inconsistent shapes")
    head["meta"] = meta
    _CACHE[path] = (stamp, head)
    return head


def adjust(head: Mapping[str, Any], served_model: Any, hidden: Any, n: int) -> list:
    """The head's term for the first `n` labels, after checking the reply fits the head."""
    import numpy as np
    want = head["meta"]["served_model"]
    if served_model != want:
        raise HeadError(f"decision head was trained on {want!r}; this reply came from {served_model!r}")
    if not isinstance(hidden, Mapping) or hidden.get("dtype") != "float32" or not hidden.get("b64"):
        raise HeadError("the served model returned no hidden state (decision heads need Strata's strata_hidden)")
    h = np.frombuffer(base64.b64decode(hidden["b64"]), dtype="<f4").astype(np.float64)
    if h.shape != head["mu"].shape:
        raise HeadError(f"hidden state has {h.size} values; the head expects {head['mu'].size}")
    if n > head["U"].shape[1]:
        raise HeadError(f"the head covers {head['U'].shape[1]} labels; the question has {n}")
    return (((h - head["mu"]) / head["sd"]) @ head["U"][:, :n] + head["c"][:n]).tolist()
