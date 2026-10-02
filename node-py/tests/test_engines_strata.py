"""The Strata adapter, against a FAKE Strata server (the serve server's API
shape): `/health` and `/v1/chat/completions`, and a refusal dialect that is its
own.

What is pinned here is the honesty rules from design §1-§2 plus the REAL launch
shape of the pinned rev: the entry point is `serve/server.py` with the engine
config (setup's `run-<model>.sh` runs the same thing), the unit-measured-cost
answer is `unknown` (Strata's startup prints NO engine-memory report — silence
is not a measurement), and thinking/tools/vision/context_len are never
invented. The REAL engine is verified against the pinned rev in task 5.2
(`strata.sh verify`) — this file cannot stand in for that and does not try to.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from engines.strata import DEFAULT_MAX_CONCURRENT, StrataEngine


class _FakeStrata(BaseHTTPRequestHandler):
    """The serve server's API surface, as far as the adapter and the proxy see
    it (its module docstring: GET /health, GET /v1/models, POST
    /v1/chat/completions — OpenAI-compatible)."""
    refuse_long_prompts = False
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/health") or self.path.endswith("/v1/models"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path.endswith("/status"):
            out = json.dumps({"busy": False, "queued": 0}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _FakeStrata.seen.append(body)
        if _FakeStrata.refuse_long_prompts:
            out = json.dumps({"error": {
                "message": "the prompt exceeds the available context size"}}).encode()
            self.send_response(400)
        else:
            out = json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion",
                "model": "qwen/Q2_0", "choices": [{"index": 0, "finish_reason": "stop",
                                                   "message": {"role": "assistant",
                                                               "content": "4"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1,
                          "total_tokens": 13}}).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def fake_strata():
    _FakeStrata.refuse_long_prompts = False
    _FakeStrata.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeStrata)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def _spec(port, tmp_path, **extra):
    root = tmp_path / "strata"
    root.mkdir(parents=True, exist_ok=True)
    (root / "STRATA_VERSION").write_text("36fa455\n")
    (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "serve").mkdir(exist_ok=True)
    (root / "strata-qwen-q2_0.json").write_text(json.dumps(
        {"exe": "engine/strata", "args": ["--mtp", "pack/mtp.gguf"]}))
    return {"name": "flash_next", "model": "qwen/Q2_0",
            "strata_root": str(root), "port": port,
            "attributes": dict(extra.pop("attributes", {})), **extra}


def test_the_launch_line_drives_the_repos_own_server(tmp_path):
    spec = _spec(8191, tmp_path)
    argv = StrataEngine().argv(spec, None)
    root = spec["strata_root"]
    assert argv == [
        f"{root}/.venv/bin/python",
        f"{root}/serve/server.py",
        "--engine", "strata",
        "--config", f"{root}/strata-qwen-q2_0.json",
        "--port", "8191",
        "--host", "127.0.0.1",
    ]


def test_an_explicit_config_wins(tmp_path):
    spec = _spec(8191, tmp_path, strata_config="/etc/harmony/strata-prod.json")
    assert StrataEngine().argv(spec, None)[
        StrataEngine().argv(spec, None).index("--config") + 1] == \
        "/etc/harmony/strata-prod.json"


def test_extra_args_ride_verbatim(tmp_path):
    toks = ["--api-key", "sekret", "--fit-max-tokens"]
    spec = _spec(8191, tmp_path, extra_args=toks)
    assert StrataEngine().argv(spec, None)[-3:] == toks


def test_no_budget_translation_a_self_sizing_engine_takes_the_whole_card(tmp_path):
    spec = _spec(8191, tmp_path)
    # Budget or not, the launch line is the same: Strata sizes its cache to the
    # free VRAM itself (the planner charges the whole device via
    # `exclusive_device` — planner-side, not a launch-line translation).
    assert StrataEngine().argv(spec, {"vram_bytes": 11 << 30}) == \
        StrataEngine().argv(spec, None)


def test_ready_asks_the_fake_server(fake_strata, tmp_path):
    spec = _spec(fake_strata, tmp_path)
    assert StrataEngine().ready(spec) is True
    assert StrataEngine().ready(_spec(1, tmp_path)) is False


def test_measure_is_unknown_not_the_prior(tmp_path):
    spec = _spec(8191, tmp_path)
    row = StrataEngine().measure(spec, None, None, [])
    # Strata's startup prints no engine-memory report. The answer says so —
    # never 0, never the declared prior presented as a measurement.
    assert row["measured"] == "unknown"
    assert row["source"] == "strata-startup"
    assert row["unmatched"]


def test_the_refusal_dialect_is_recognised_and_nothing_else_is(tmp_path):
    eng = StrataEngine()
    long_prompt = b'{"error":{"message":"the prompt exceeds the available context size"}}'
    assert eng.context_refusal(400, long_prompt) is not None
    assert eng.context_refusal(500, b'{"error":{"message":"prompt is too long"}}') is not None
    # Every other 4xx passes through untouched.
    assert eng.context_refusal(400, b'{"error":{"message":"bad request"}}') is None
    assert eng.context_refusal(401, long_prompt) is None


def test_rev_reads_the_pinned_version_file(tmp_path):
    spec = _spec(8191, tmp_path)
    assert StrataEngine().rev(spec) == "36fa455"
    # A declared pin that MATCHES the install is accepted (and recorded).
    assert StrataEngine().rev({**spec, "engine_rev": "36fa455"}) == "36fa455"


def test_concurrency_is_the_servers_own_limit_unless_declared(tmp_path):
    # The serve server runs ONE sequence at a time behind a FIFO at the pinned
    # rev — so 1, and a units file that knows a build that parallelises may say
    # so. Never invented, never a zero.
    spec = _spec(8191, tmp_path)
    assert StrataEngine().launch_attributes(spec)["max_concurrent"] == \
        DEFAULT_MAX_CONCURRENT == 1
    spec2 = _spec(8191, tmp_path, attributes={"max_concurrent": 4})
    assert StrataEngine().launch_attributes(spec2)["max_concurrent"] == 4


def test_lifecycle_flags_are_refused_not_silently_dropped(tmp_path):
    # --idle-unload / --lazy / --before-load hand the lifecycle back to the
    # engine; Harmony owns residency (design §0). A deployment that asked for
    # one must be TOLD it did not get it.
    for flag in ("--idle-unload", "--lazy", "--before-load"):
        spec = _spec(8191, tmp_path, extra_args=[flag, "30"])
        with pytest.raises(ValueError, match=flag):
            StrataEngine().argv(spec, None)


def test_ready_needs_health_AND_status(fake_strata, tmp_path):
    # `/health` answering is not a model that serves; `ready` asks /status too
    # (what the model is doing right now) and requires its state.
    class _HealthOnly(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("/health"):
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    import threading
    from http.server import HTTPServer
    srv = HTTPServer(("127.0.0.1", 0), _HealthOnly)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    spec = _spec(srv.server_address[1], tmp_path)
    assert StrataEngine().ready(spec) is False
    srv.shutdown()
    assert StrataEngine().ready(_spec(fake_strata, tmp_path)) is True


def test_measure_reports_what_it_got_and_names_what_it_never_got(tmp_path):
    from engines import LineCapture
    spec = _spec(8191, tmp_path)
    cap = LineCapture()
    cap.feed("[strata] experts loaded 34.10 GiB at 12.3 GiB/s\n")
    cap.feed("[strata] expert cache 8 slots, 12.5 GiB\n")
    # No NVML row for this process (it holds no VRAM) -> no footprint, and the
    # row says so; the /proc numbers and the engine's lines ARE carried.
    row = StrataEngine().measure(spec, cap, None, [])
    assert row["source"] == "strata-startup"
    assert len(row["engine_lines"]) == 2
    assert row["measured"] == "unknown"       # silence about VRAM is not a zero
    assert "footprint" not in row
    # With a pid, the /proc numbers come through.
    row2 = StrataEngine().measure(spec, cap, os.getpid(), [])
    assert "ram_pinned_bytes" in row2 and "ram_mapped_bytes" in row2
    # Nothing at all: every missing source is NAMED.
    row3 = StrataEngine().measure(spec, LineCapture(), None, [])
    assert row3["measured"] == "unknown"
    assert row3["unmatched"] == ["no engine memory report", "no NVML reading",
                                 "no slot/GiB log line"]


def test_the_pinned_proc_field_is_chosen_by_a_positive_control():
    # The engine mlock()s what it pins for the GPU. Spawn a child, read its
    # /proc fields, have it mlock 4 MB, read again — and prove WHICH field
    # shows the pin: VmLck rises by the pin, RssFile does not (a locked
    # anonymous buffer is not file-backed). That is the positive control
    # behind `ram_pinned_bytes` = VmLck. 4 MB because the default
    # RLIMIT_MEMLOCK hard limit is 8 MB — the control must run everywhere.
    import subprocess
    import sys
    code = ("import ctypes, resource, sys, time\n"
            "resource.setrlimit(resource.RLIMIT_MEMLOCK, "
            "(resource.getrlimit(resource.RLIMIT_MEMLOCK)[1],) * 2)\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "buf = ctypes.create_string_buffer(4 * (1 << 20))\n"
            "assert ctypes.CDLL('libc.so.6').mlock(buf, 4 * (1 << 20)) == 0, 'mlock refused'\n"
            "print('locked', flush=True)\n"
            "time.sleep(10)\n")
    child = subprocess.Popen([sys.executable, "-c", code],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        before = StrataEngine._proc_status_bytes(child.pid)
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "locked"
        after = StrataEngine._proc_status_bytes(child.pid)
        assert before[0] is not None and after[0] is not None
        assert after[0] - before[0] >= 4 * (1 << 20), (
            f"VmLck must show the pin: {before[0]} -> {after[0]}")
        assert (after[1] or 0) - (before[1] or 0) < 1 * (1 << 20), (
            f"RssFile must NOT show an anonymous pin: {before[1]} -> {after[1]}")
    finally:
        child.kill()
        child.wait(timeout=5)


def test_the_engine_source_pin_is_checked_at_startup(tmp_path):
    spec = _spec(8191, tmp_path, engine_source={"rev": "1111111"})
    with pytest.raises(ValueError, match="engine_source.rev"):
        StrataEngine().rev(spec)          # what is installed is 36fa455 (the fixture)
    ok = _spec(8191, tmp_path, engine_source={"rev": "36fa455"})
    assert StrataEngine().rev(ok) == "36fa455"
    # The model hash is checked against the sidecar when it exists.
    ok2 = _spec(8191, tmp_path, engine_source={"rev": "36fa455", "sha256": "beef"})
    (Path(ok2["strata_root"]) / "MODEL_SHA256").write_text("deadbeef\n")
    with pytest.raises(ValueError, match="sha256"):
        StrataEngine().rev(ok2)
