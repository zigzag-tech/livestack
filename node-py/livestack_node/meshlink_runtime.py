"""Where livestack gets the meshlink Python packages — one explicit, validated
answer instead of an implicit ``sys.path`` entry.

``mesh://`` peers need three importable things from the meshlink checkout pinned
by ``MESHLINK.lock``: ``mesh_outbound_py`` (pure Python), ``mesh_route_py`` (a
pyo3 extension: it must be BUILT) and ``websockets``. A hostd without them used
to learn so one request at a time: every ``POST /peers`` announcing a ``mesh://``
URL raised from ``MeshPeer.__init__`` and surfaced as HTTP 500 plus a full
traceback (measured 2026-11-02, xc-tower-ubuntu: ~1,200 per hour, ~20/min, for
days, while the node's http announce kept the roster looking healthy).

This module makes that state a *named, bounded* condition:

* ``LIVESTACK_MESHLINK_DIR`` — the meshlink checkout. Validated: it must hold
  ``packages/mesh_outbound_py``; its ``packages/mesh_outbound_py`` is put on
  ``sys.path`` and, when ``mesh_route_py`` is not otherwise installed, the newest
  cargo-built ``rust/target/*/deps/libmesh_route_py.so`` is loaded as that
  module. A wheel installed in the venv works with no setting at all.
* :func:`status` — ``{"available", "missing", "reason", "meshlink_dir"}``,
  published on hostd ``GET /status`` and logged once at startup.
* :class:`MeshlinkUnavailable` — the single exception for "cannot dial mesh://",
  carrying ``degradation = "meshlink_unavailable"``; hostd answers it as 503.
* :class:`RateLimitedLog` — one line per reason per window with a suppressed
  count, so a retrying announcer cannot turn the journal into the symptom.
"""
from __future__ import annotations

import glob
import importlib
import importlib.util
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Mapping, Optional

DIR_ENV = "LIVESTACK_MESHLINK_DIR"
REQUIRED_ENV = "LIVESTACK_MESHLINK_REQUIRED"
#: Import names, in the order they are checked. The first two are meshlink's.
MODULES = ("mesh_route_py", "mesh_outbound_py", "websockets")
_BUILD_HINT = ("build it with `maturin build --release` in "
               "<meshlink>/rust/mesh-route-py (or `cargo build -p mesh-route-py`) "
               "and install the wheel into this interpreter's venv")


class MeshlinkUnavailable(RuntimeError):
    """The meshlink packages cannot be imported here, so no ``mesh://`` peer can
    be dialled. A RuntimeError so older callers that caught that keep working."""

    degradation = "meshlink_unavailable"

    def __init__(self, reason: str, missing: List[str]):
        super().__init__(reason)
        self.reason = reason
        self.missing = list(missing)


def _checkout_problem(root: str) -> Optional[str]:
    if not os.path.isdir(root):
        return f"{DIR_ENV}={root!r} is not a directory"
    if not os.path.isfile(os.path.join(
            root, "packages", "mesh_outbound_py", "mesh_outbound_py", "mux.py")):
        return (f"{DIR_ENV}={root!r} is not a meshlink checkout "
                "(packages/mesh_outbound_py/mesh_outbound_py/mux.py not found)")
    return None


def _load_built_route_module(root: str) -> Optional[str]:
    """Load the newest cargo-built mesh_route_py from `root`; the problem or None."""
    found = sorted(glob.glob(os.path.join(root, "rust", "target", "*", "deps",
                                          "libmesh_route_py.so")),
                   key=os.path.getmtime)
    if not found:
        return f"no built mesh_route_py under {root}/rust/target — {_BUILD_HINT}"
    spec = importlib.util.spec_from_file_location("mesh_route_py", found[-1])
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        return f"{found[-1]} failed to load: {type(e).__name__}: {e}"
    sys.modules["mesh_route_py"] = module
    return None


def probe(env: Optional[Mapping[str, str]] = None) -> Dict[str, object]:
    """Apply ``LIVESTACK_MESHLINK_DIR`` and report what imports. Idempotent;
    never raises — an unavailable meshlink is a status, not an exception."""
    env = os.environ if env is None else env
    root = (env.get(DIR_ENV) or "").strip()
    problems: List[str] = []
    if root:
        bad = _checkout_problem(root)
        if bad:
            problems.append(bad)
        else:
            pkg = os.path.join(root, "packages", "mesh_outbound_py")
            if pkg not in sys.path:
                sys.path.insert(0, pkg)
            if importlib.util.find_spec("mesh_route_py") is None:
                bad = _load_built_route_module(root)
                if bad:
                    problems.append(bad)
    missing: List[str] = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if "mesh_route_py" in missing and not any("mesh_route_py" in p for p in problems):
        problems.append("mesh_route_py is a compiled extension and is not installed — "
                        + _BUILD_HINT)
    if "mesh_outbound_py" in missing and not root:
        problems.append(f"mesh_outbound_py not importable; set {DIR_ENV} to the pinned "
                        "meshlink checkout (MESHLINK.lock) or install its package")
    available = not missing and not problems
    return {"available": available, "missing": missing,
            "reason": None if available else "; ".join(problems) or
            "cannot import " + ", ".join(missing),
            "meshlink_dir": root or None}


def require() -> None:
    """Raise :class:`MeshlinkUnavailable` naming what is missing, or return."""
    st = probe()
    if not st["available"]:
        raise MeshlinkUnavailable(
            f"meshlink packages unavailable ({', '.join(st['missing']) or 'invalid setting'}): "
            f"{st['reason']}", st["missing"])  # type: ignore[arg-type]


def startup_check(log: Callable[[str], None] = print,
                  env: Optional[Mapping[str, str]] = None) -> Dict[str, object]:
    """Run once at process start. Logs ONE line either way. With
    ``LIVESTACK_MESHLINK_REQUIRED=1`` an unavailable meshlink stops the process
    here, with that line, instead of 500ing every later ``mesh://`` announce."""
    env = os.environ if env is None else env
    st = probe(env)
    if st["available"]:
        log(f"[hostd] meshlink ready (dir={st['meshlink_dir'] or 'site-packages'})")
        return st
    line = (f"[hostd] meshlink UNAVAILABLE — mesh:// peers will be refused with 503 "
            f"meshlink_unavailable: {st['reason']}")
    if (env.get(REQUIRED_ENV) or "").strip().lower() in ("1", "true", "yes", "on"):
        raise SystemExit(line + f" ({REQUIRED_ENV} is set)")
    log(line)
    return st


class RateLimitedLog:
    """At most one line per key per ``window_s``; the next line carries how many
    were suppressed. Thread-safe."""

    def __init__(self, log: Callable[[str], None] = print, window_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self._log, self._window, self._clock = log, window_s, clock
        self._lock = threading.Lock()
        self._state: Dict[str, List[float]] = {}  # key -> [last_emit, suppressed]

    def __call__(self, key: str, message: str) -> bool:
        now = self._clock()
        with self._lock:
            st = self._state.get(key)
            if st is not None and now - st[0] < self._window:
                st[1] += 1
                return False
            suppressed = int(st[1]) if st else 0
            self._state[key] = [now, 0]
        suffix = f" (+{suppressed} suppressed in the last window)" if suppressed else ""
        self._log(message + suffix)
        return True
