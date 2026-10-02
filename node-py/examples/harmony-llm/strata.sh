#!/bin/sh
# Strata deployment helper (openspec/changes/harmony-offload-engine-units).
#
#   strata.sh setup    the PINNED install: rev from STRATA_VERSION, the gcc-14
#                      nvcc drop-in this host needs, the size/context the unit
#                      declares. Resumable — everything already done is kept.
#   strata.sh verify   the API shape the proxy depends on (task 5.2): OpenAI
#                      `tool_calls`, `usage`, and `stream_options.include_usage`
#                      in a streamed answer, plus the pinned rev and the process
#                      actually serving the port. Exit 0 only when every check
#                      passes; each check PRINTS its verdict (absence and
#                      failure must not look alike).
#
# Run it inside the checkout (~/strata/strata). PORT is the serve port to
# verify against (default 8191).
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
PORT="${PORT:-8191}"
REV="$(cat STRATA_VERSION 2>/dev/null || echo unknown)"

case "${1:-}" in
setup)
    # --no-start: Harmony owns the lifecycle; the unit starts the engine
    # through its adapter. The gcc-14 drop-in is HARMONY.md's fp8 note: CUDA
    # 12.9 nvcc cannot identify itself under gcc 15.
    exec env NVCC_PREPEND_FLAGS="${NVCC_PREPEND_FLAGS:--ccbin /usr/bin/g++-14}" \
             CUDAHOSTCXX="${CUDAHOSTCXX:-/usr/bin/g++-14}" \
        ./setup.sh --yes --family qwen --model "${MODEL:-Q2_0}" \
                   --context "${CONTEXT:-131072}" --vision none --no-start \
                   --port "$PORT"
    ;;
verify)
    exec "$ROOT/.venv/bin/python" - "$PORT" "$REV" <<'PY'
import json, sys, urllib.request

port, rev = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"
ok = True

def check(name, passed, detail=""):
    global ok
    print(f"[{'ok' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""),
          flush=True)
    ok = ok and passed

def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, json.loads(r.read() or b"{}")

def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.read()

try:
    st, _ = get("/health")
    check("GET /health", st == 200, f"status {st}")
except Exception as e:
    check("GET /health", False, f"{type(e).__name__}: {e}")
    print("the engine is not answering; nothing else can be verified", flush=True)
    sys.exit(1)

try:
    st, models = get("/v1/models")
    check("GET /v1/models", st == 200 and models, f"status {st}")
except Exception as e:
    check("GET /v1/models", False, f"{type(e).__name__}: {e}")

# 1. tool_calls — the shape the Overlord's tool schemas need. The prompt asks
# for the tool BY NAME so a build with tools answers with one.
tool = {"type": "function", "function": {
    "name": "get_weather", "description": "Get the weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}
try:
    _, out = post("/v1/chat/completions", {
        "model": "require:class=llm",
        "messages": [{"role": "user",
                      "content": "Use the get_weather tool to report Paris. "
                                 "Call the tool; do not answer directly."}],
        "tools": [tool], "tool_choice": "auto"})
    calls = ((out.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
    check("tool_calls", bool(calls),
          json.dumps(calls)[:120] if calls else f"no tool_calls in {json.dumps(out)[:120]}")
except Exception as e:
    check("tool_calls", False, f"{type(e).__name__}: {e}")

# 2. usage on a plain answer.
try:
    _, out = post("/v1/chat/completions", {
        "model": "require:class=llm",
        "messages": [{"role": "user", "content": "Say OK."}]})
    u = out.get("usage") or {}
    check("usage", bool(u.get("prompt_tokens") is not None
                        and u.get("completion_tokens") is not None),
          json.dumps(u))
except Exception as e:
    check("usage", False, f"{type(e).__name__}: {e}")

# 3. stream_options.include_usage — a streamed answer that ends with a usage
# chunk before [DONE].
try:
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps({"model": "require:class=llm", "stream": True,
                         "stream_options": {"include_usage": True},
                         "messages": [{"role": "user", "content": "Say OK."}]}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8", "replace")
    usage_line = next((l for l in text.splitlines()
                       if l.startswith("data: ") and '"usage"' in l), "")
    check("stream_options.include_usage", bool(usage_line),
          usage_line[:120] or "no usage chunk in the stream")
except Exception as e:
    check("stream_options.include_usage", False, f"{type(e).__name__}: {e}")

# What is ACTUALLY serving the port, and which rev it was pinned to. The
# process name is a fact about the deployment, not a promise in a doc.
try:
    import subprocess
    out = subprocess.run(["fuser", f"{port}/tcp"], capture_output=True, text=True).stdout
    pids = out.split()
    names = []
    for pid in pids[:3]:
        try:
            names.append(open(f"/proc/{pid}/cmdline", "rb").read()
                         .replace(b"\0", b" ").decode()[:100])
        except OSError:
            pass
    check("process on the port", bool(names), " | ".join(names) or "none found")
except Exception as e:
    check("process on the port", False, f"{type(e).__name__}: {e}")

print(f"strata rev: {rev}", flush=True)
sys.exit(0 if ok else 1)
PY
    ;;
*)
    echo "usage: strata.sh setup|verify   (PORT=8191, MODEL=Q2_0, CONTEXT=131072)" >&2
    exit 2
    ;;
esac
