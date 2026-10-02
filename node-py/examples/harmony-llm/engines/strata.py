"""The Strata engine: a llama.cpp-lineage C++/CUDA core behind Strata's own
OpenAI-compatible server, with a whole-device appetite and a host-RAM
residency.

`architectds/Strata` at the rev pinned in `STRATA_VERSION` beside the checkout
(upstream README points at Niko1221/Strata; `setup.sh --yes --family qwen
--model <size>` handles download, patch and build). Three facts drive
everything here:

* **The model stays mapped in host RAM.** VRAM holds only the compute state and
  the KV cache — and the cache is sized to whatever VRAM is free. That is why
  the unit is `exclusive_device: true` (the planner charges it the whole card)
  and why its footprint carries `ram_bytes`: planning only its VRAM is how a
  host swaps.
* **The launch is the repo's own server, not a bare engine binary.** The
  canonical start (what setup writes into `run-<model>.sh`) is
  `.venv/bin/python serve/server.py --engine strata --config strata-*.json
  --port N` — the engine command line lives INSIDE the config (exe, args, cwd,
  tokenizer, model_name) and this adapter drives the server that reads it.
  Design §1 sketched `./llama-server ...`; the pinned rev's actual script name
  is what runs, because "whatever the repo's actual script name, the adapter
  owns the exact argv".
* **Strata's startup prints no engine-memory report.** `unit-measured-cost`
  reports `measured: "unknown"` and the DECLARED numbers stand. Silence is not
  a measurement. MTP speculative decode is built in — never an attribute.

`thinking`/`tools`/`vision` are NOT invented: they are what the units file
declared, or False — only the operator knows what this build serves, and
`tests/test_engines_attributes.py` pins that truth. The served `context_len` is
declared for the same reason: it is baked into the engine config at setup time
(`--context`), not on the server's command line.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Mapping, Optional

import httpx

from . import Engine, LineCapture, terminate_and_wait

# The serve server runs ONE sequence at a time behind a FIFO (its own module
# docstring at the pinned rev) — so a Strata unit admits one generation at a
# time unless its units file, which knows the build, says otherwise. This
# becomes a PLANNER attribute (the queue below a saturated engine is bounded by
# it), so it is written down, not guessed per call.
DEFAULT_MAX_CONCURRENT = 1

# Flags that hand the LIFECYCLE back to the engine: it would unload on idle,
# start unloaded, or run a command before a reload — all decisions Harmony owns
# (residency is the planner's). Refused at launch rather than silently dropped,
# because a deployment that asked for one needs to be told it did not get it.
OWNED_BY_HARMONY = ("--idle-unload", "--lazy", "--before-load")


def _argv_tokens(spec: dict) -> "list[str]":
    args = spec.get("extra_args")
    return args if isinstance(args, list) else shlex.split(str(args or ""))


def _bytes_from_gib(text: str) -> "int | None":
    """`12.34 GiB` / `12.34GB` -> bytes. None when the text names no size."""
    import re
    m = re.search(r"([\d.]+)\s*(GiB|GB)", text)
    return int(float(m.group(1)) * (1 << 30)) if m else None


class StrataEngine(Engine):
    name = "strata"

    def root(self, spec: dict) -> str:
        return (str(spec.get("strata_root") or "").strip()
                or os.environ.get("HARMONY_STRATA_ROOT", "").strip()
                or os.path.expanduser("~/strata/strata"))

    def config_path(self, spec: dict) -> str:
        """The run config setup wrote (`strata-*.json` in the root: exe, args,
        cwd, tokenizer, model_name). Explicit `strata_config` wins, then the
        env, then the newest one there — the same file `run-<model>.sh` runs,
        because that file IS this engine's launch line."""
        explicit = str(spec.get("strata_config") or "").strip()
        if explicit:
            return explicit
        env = os.environ.get("HARMONY_STRATA_CONFIG", "").strip()
        if env:
            return env
        root = self.root(spec)
        try:
            cands = sorted((os.path.join(root, n) for n in os.listdir(root)
                            if n.startswith("strata-") and n.endswith(".json")),
                           key=os.path.getmtime, reverse=True)
        except OSError:
            cands = []
        return cands[0] if cands else os.path.join(root, "strata-missing.json")

    def _python(self, spec: dict) -> str:
        return os.path.join(self.root(spec), ".venv", "bin", "python")

    # -- what to launch ----------------------------------------------------
    def argv(self, spec: dict, budget: "Mapping[str, float] | None" = None,
             total_bytes: Optional[float] = None) -> "list[str]":
        # NO BUDGET TRANSLATION: Strata sizes its cache to whatever VRAM is free
        # when it starts, and with `exclusive_device` the grant is the whole
        # device anyway. An engine that self-sizes must not be told a fraction.
        toks = _argv_tokens(spec)
        bad = [f for f in OWNED_BY_HARMONY
               if any(t == f or t.startswith(f + "=") for t in toks)]
        if bad:
            raise ValueError(
                f"unit {spec.get('name')!r}: {', '.join(bad)} hand the lifecycle "
                f"back to the engine; Harmony owns residency (design §0). "
                f"Remove them from extra_args.")
        return [
            self._python(spec),
            os.path.join(self.root(spec), "serve", "server.py"),
            "--engine", "strata",
            "--config", self.config_path(spec),
            "--port", str(spec["port"]),
            "--host", "127.0.0.1",
        ] + toks

    def ready(self, spec: dict, timeout: float = 2.0) -> bool:
        """Up AND loaded: `/health` answering, and `/status` (what the model is
        doing right now) reporting its state — a port that answers is not the
        same as a model that serves."""
        try:
            r = httpx.get(f"http://127.0.0.1:{spec['port']}/health", timeout=timeout)
            if r.status_code != 200:
                return False
            s = httpx.get(f"http://127.0.0.1:{spec['port']}/status", timeout=timeout)
            return s.status_code == 200 and isinstance(s.json(), dict) \
                and "busy" in s.json()
        except Exception:
            return False

    def measure(self, spec: dict, capture, pid: "int | None",
                argv: "list[str]") -> Optional[dict]:
        """What this engine run COSTS, from three sources — and `unknown` (with
        what never came named) when none of them answers. Never 0, never the
        declared prior presented as a measurement.

        * the ENGINE's own lines (`loaded … GiB at …`, `expert cache … slots`),
        * NVML: the VRAM this pid's engine holds (nvidia-smi, no torch import —
          a first CUDA call in this process would itself take card space),
        * `/proc/<pid>/status`: `VmLck` (what the engine mlock()s for the GPU —
          the PINNED field, chosen by the positive control in
          `tests/test_engines_strata.py`) and `RssFile` (the mapped GGUF).

        The row's `source` is `strata-startup` either way: this is what the
        engine's startup is willing to say, and what it will not say is the
        `unmatched` list.
        """
        lines = list(getattr(capture, "lines", []) or [])
        matched = [ln.strip() for ln in lines
                   if (" loaded " in ln and "GiB at" in ln)
                   or ("expert cache " in ln and " slots, " in ln)]
        vram = self._pid_vram_bytes(pid)
        pinned, mapped = self._proc_status_bytes(pid)
        row: dict = {"measured": "unknown", "source": "strata-startup",
                     "unmatched": ["no engine memory report: Strata's startup prints none"]}
        if matched:
            row["engine_lines"] = matched[:8]
            if row["unmatched"] == ["no engine memory report: Strata's startup prints none"]:
                row["unmatched"] = []
        if pinned is not None:
            row["ram_pinned_bytes"] = pinned
        if mapped is not None:
            row["ram_mapped_bytes"] = mapped
        if vram is not None:
            # The card claim: compute state + the KV cache sized to the free
            # VRAM. One number both times — a self-sizing cache has no separate
            # minimum.
            row.update({"measured": "strata-startup", "footprint": vram,
                        "min_footprint": vram})
        if row["unmatched"] and vram is None and not matched:
            row["unmatched"] = ["no engine memory report", "no NVML reading",
                                "no slot/GiB log line"]
        return row

    @staticmethod
    def _pid_vram_bytes(pid: "int | None") -> "int | None":
        """VRAM this pid's engine holds, via nvidia-smi. None when unknowable —
        an unreadable meter contributes nothing, it never fabricates a zero."""
        if not pid:
            return None
        import subprocess
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2 and parts[0] == str(pid):
                    return int(float(parts[1]) * (1 << 20))
        except Exception:
            return None
        return None

    @staticmethod
    def _proc_status_bytes(pid: "int | None"):
        """(`VmLck`, `RssFile`) from /proc/<pid>/status, in bytes.

        `VmLck` is what the engine mlock()s for the GPU — the PINNED memory —
        and `RssFile` is the file-backed mapping (the GGUF). Both, because the
        two numbers answer different questions ("how much RAM is committed" vs
        "how much of the model is resident") and only one of them is the pin."""
        if not pid:
            return None, None
        locked = mapped = None
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmLck:"):
                        locked = int(line.split()[1]) * 1024
                    elif line.startswith("RssFile:"):
                        mapped = int(line.split()[1]) * 1024
        except OSError:
            return None, None
        return locked, mapped

    # -- what the launch line means -----------------------------------------
    def launch_attributes(self, spec: dict) -> dict:
        """`class=llm` is the only DERIVED fact — the endpoint shape is fixed
        (OpenAI chat/completions).

        Everything else is DECLARED or absent: `thinking`/`tools`/`vision` (a
        Strata build's chat template and tool parser are its own business),
        `context_len` (baked into the engine config at setup time, not on this
        command line), and `max_concurrent` (the server runs one sequence at a
        time behind a FIFO unless the build says otherwise). An attribute that
        lies is worse than one that is missing — silence is not a yes.
        """
        attrs = dict(spec.get("attributes") or {})
        attrs["class"] = "llm"
        for key in ("thinking", "tools", "vision"):
            attrs[key] = bool(attrs.get(key, False))
        if "context_len" not in attrs:
            attrs.pop("context_len", None)
        try:
            declared = int(attrs.get("max_concurrent") or 0)
        except (TypeError, ValueError):
            declared = 0
        attrs["max_concurrent"] = declared if declared > 0 else DEFAULT_MAX_CONCURRENT
        return attrs

    # -- how it dies --------------------------------------------------------
    def stop(self, proc) -> None:
        terminate_and_wait(proc, "strata")

    # -- refusal semantics ---------------------------------------------------
    def context_refusal(self, status: int, body: bytes) -> Optional[str]:
        # A prompt past the window is refused in the engine's own dialect; the
        # routing consequence is the server's. Anything else 4xx passes through
        # untouched.
        if status not in (400, 413, 500):
            return None
        text = body.decode("utf-8", "replace").lower()
        if (("context" in text or "prompt" in text)
                and ("exceed" in text or "too long" in text or "too big" in text)):
            return body.decode("utf-8", "replace")
        return None

    def rev(self, spec: dict) -> str:
        """The pinned rev for the ledger record: `engine_source.rev` (the units
        file's pin), else `engine_rev`, else `STRATA_VERSION` beside the
        checkout.

        A DECLARED pin that disagrees with what is installed is a STARTUP ERROR
        naming both — a load record claiming a rev that was not loaded is worse
        than no record at all. Same for `engine_source.sha256` against the
        model's `MODEL_SHA256` sidecar, when either exists."""
        src = spec.get("engine_source")
        src = src if isinstance(src, dict) else {}
        want = str(src.get("rev") or spec.get("engine_rev") or "").strip()
        have = self._version_file(spec)
        if want and have and not (have.startswith(want) or want.startswith(have)):
            raise ValueError(
                f"unit {spec.get('name')!r}: engine_source.rev {want} does not match "
                f"the installed engine {have} (STRATA_VERSION); re-pin or re-install")
        want_hash = str(src.get("sha256") or src.get("model_sha256") or "").strip()
        if want_hash:
            try:
                with open(os.path.join(self.root(spec), "MODEL_SHA256"),
                          "r", encoding="utf-8") as fh:
                    have_hash = fh.read().strip().split()[0]
            except OSError:
                have_hash = ""
            if have_hash and have_hash != want_hash:
                raise ValueError(
                    f"unit {spec.get('name')!r}: engine_source.sha256 {want_hash[:16]} "
                    f"does not match the model on disk {have_hash[:16]}")
        return want or have

    def _version_file(self, spec: dict) -> str:
        root = self.root(spec)
        for path in (os.path.join(root, "STRATA_VERSION"),
                     os.path.join(root, "strata", "STRATA_VERSION")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                continue
        return ""
