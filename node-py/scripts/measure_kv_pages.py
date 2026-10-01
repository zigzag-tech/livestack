"""Measure KV pages per running sequence on a live vLLM, directly.

    python scripts/measure_kv_pages.py <kv_tokens> <block_size>

Sends bursts of K identical requests (662-token prompt, 300 forced output
tokens, lowest scheduling priority) and reads `vllm:kv_cache_usage_perc` twice
a second, keeping only readings where EXACTLY K requests run and none wait, so
concurrent production traffic is excluded rather than averaged in. Reports
pages per sequence (the slope) and the state pages beyond the attention pages
`composition_replay.kv_need` would charge for the same request.

2026-10-01 on llm_general (27B, chips+jemm): fp8 2.94 pages of 1,568 tokens
(state 1.94), bf16 2.22 state on 4.22 pages of 784. Recorded on the measured
rows; see _plans/composition-replay-validation.md. Use a quiet moment: each
burst occupies up to K sequences for ~12 s.
"""
import json, sys, threading, time, urllib.request, statistics
BASE = "http://127.0.0.1:8189"
KV_TOKENS, BLOCK = int(sys.argv[1]), int(sys.argv[2])
PROMPT = "The quick brown fox jumps over the lazy dog. " * 65          # ~650 tokens

def metrics():
    m = {}
    for l in urllib.request.urlopen(BASE + "/metrics", timeout=5).read().decode().splitlines():
        if l.startswith("vllm:num_requests_running{"): m["run"] = float(l.rsplit(" ", 1)[1])
        if l.startswith("vllm:num_requests_waiting{"): m["wait"] = float(l.rsplit(" ", 1)[1])
        if l.startswith("vllm:kv_cache_usage_perc{"): m["kv"] = float(l.rsplit(" ", 1)[1])
    return m

def one(out):
    body = {"model": "llm_general", "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 300, "ignore_eos": True, "temperature": 0, "priority": 100,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out.append(json.load(r)["usage"])

results = {}
for K in (1, 2, 4, 6, 8):
    while metrics().get("run", 0) > 0:          # start from an idle engine
        time.sleep(0.5)
    out = []
    ths = [threading.Thread(target=one, args=(out,)) for _ in range(K)]
    for t in ths: t.start()
    pts = []
    while any(t.is_alive() for t in ths):
        m = metrics()
        if m.get("run") == K and m.get("wait") == 0:
            pts.append(m["kv"] * KV_TOKENS / BLOCK)
        time.sleep(0.5)
    for t in ths: t.join()
    # Pages grow as decode proceeds; take the median of exact-K readings.
    results[K] = (statistics.median(pts) if pts else None, len(pts), out[0]["prompt_tokens"] if out else None)
    print(f"K={K}: pages={results[K][0]} over {len(pts)} exact readings, prompt_tokens={results[K][2]}", flush=True)
ks = [k for k in results if results[k][0] is not None]
mx = sum(ks) / len(ks); my = sum(results[k][0] for k in ks) / len(ks)
slope = sum((k - mx) * (results[k][0] - my) for k in ks) / sum((k - mx) ** 2 for k in ks)
pt = results[ks[0]][2]
attn = -(-(pt + 300) // BLOCK)                   # as kv_need counts it: prompt + completion
print(f"SLOPE pages/seq = {slope:.3f}, intercept {my - slope * mx:.3f}; attention pages/seq (kv_need convention) = {attn}; state pages/seq ~ {slope - attn:.3f}")
