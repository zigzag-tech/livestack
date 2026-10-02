"""Run paired requirement-routed requests and publish a resumable HTML gallery."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageStat


def render(prompts, out):
    rows, complete = [], 0
    samples = {"qwen": [], "z": []}
    for p in prompts:
        cards = []
        for model, name, hardware in [("qwen", "Qwen-Image-2512 · 20B", "xc-tower · RTX 3090"), ("z", "Z-Image-Turbo · 6B", "zz-Joe · RTX 2070")]:
            stem = f"{p['id']:02d}-{p['slug']}-{model}"
            meta_path = out / (stem + ".json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                m = meta["harmony"]
                samples[model].append(m)
                complete += 1
                stats = f"{m['total_s']:.1f}s total · {m['steps']} steps · {m['peak_allocated_bytes']/1e9:.2f} GB peak allocated"
                image = f'<a href="{stem}.png" target="_blank"><img src="{stem}.png" alt="{html.escape(p["title"])} — {name}" loading="lazy"></a>'
                details = f'<details><summary>Request, routing & measurements</summary><pre>{html.escape(json.dumps(meta,indent=2,ensure_ascii=False))}</pre><a href="{stem}.json">Download evidence JSON</a></details>'
            else:
                stats, image, details = "Waiting for generation", '<div class="pending">Pending</div>', ""
            cards.append(f'<article><h3>{name}</h3><div class="machine">{hardware}</div>{image}<p class="stats">{stats}</p>{details}</article>')
        rows.append(f'<section id="pair-{p["id"]}"><div class="rowtitle"><span>{p["id"]:02d}</span><h2>{html.escape(p["title"])}</h2></div><p class="prompt">{html.escape(p["prompt"])}</p><p class="seed">Identical prompt · seed {p["seed"]} · {p["width"]} × {p["height"]}</p><div class="pair">{"".join(cards)}</div></section>')
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Qwen vs Z-Image · Harmony mesh comparison</title><style>
*{box-sizing:border-box}body{margin:0;background:#f6f5f1;color:#20272b;font:16px/1.6 system-ui,sans-serif}main{max-width:1480px;margin:auto;padding:40px 28px}header{padding-bottom:30px;border-bottom:2px solid #20272b}.eyebrow{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#59705e}h1{font-size:clamp(30px,5vw,60px);line-height:1.12;letter-spacing:-.04em;margin:14px 0}header p{max-width:1000px}.progress{display:inline-block;padding:7px 14px;background:#e3ebdf;border-radius:4px;font-weight:600}.note{font-size:14px;color:#596267}section{padding:36px 0;border-bottom:1px solid #c7cdca}.rowtitle{display:flex;align-items:baseline;gap:16px}.rowtitle span{font:18px monospace;color:#6d8070}h2{font-size:26px;margin:0}.prompt{max-width:1100px;margin:12px 0}.seed,.machine,.stats{font-size:13px;color:#596267}.pair{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:16px}article{min-width:0;padding:18px;background:white;border:1px solid #d4dad5;border-radius:6px}h3{font-size:18px;margin:0}.machine{margin-bottom:12px}img{display:block;width:100%;height:auto;border-radius:3px}.pending{aspect-ratio:1;display:grid;place-items:center;background:#edf0eb;color:#7a857c}details{font-size:13px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f3;padding:12px;font-size:11px}a{color:#285d47}footer{padding:24px 0;color:#596267;font-size:13px}@media(max-width:700px){main{padding:24px 14px}.pair{grid-template-columns:1fr}article{padding:12px}}@media(prefers-color-scheme:dark){body{background:#171c1a;color:#e9ece7}article{background:#222a25;border-color:#435046}.pending,pre{background:#303a32}.note,.seed,.machine,.stats,footer{color:#abb8ad}.progress{background:#344b37}a{color:#a8d5b4}}
</style></head><body><main><header><div class="eyebrow">Harmony · mesh inference · September 2026</div><h1>Qwen vs Z-Image</h1><p>Ten prompts, two models, the same composition brief. Each image was requested through Harmony's image ingress using model requirements; the fleet planner selected the unit and machine.</p><div class="progress">COMPLETE_COUNT / 20 images generated</div><p class="note"><strong>Qwen request:</strong> <code>params_b&gt;=20</code> · <strong>Z-Image request:</strong> <code>params_b&lt;=6</code>. Qwen uses INT8 generator weights; Z-Image uses NF4. Both use CPU FP32 text encoding. Qwen uses 50 steps with CFG 4; Z-Image Turbo uses 9 steps with guidance 0. This compares these deployed configurations, including quantization, on different GPUs. Equal seeds do not imply equal noise across architectures. Timings include local admission and first-load costs where applicable.</p><p class="note">Click an image for its full-size PNG. Expand evidence below it to inspect the exact request, pinned model revision, fleet placement plan, concrete local admission grant, and memory measurements. The fleet broker observes and selects; only the selected host broker warms or evicts GPU units. Refresh to see new results.</p></header>ROWS<footer>Generated locally on your mesh. No proprietary image API used. Evidence contains no credentials.</footer></main></body></html>'''
    dest = out / "index.html"
    temp = out / f"index.html.{os.getpid()}.tmp"
    summary = '<div style="overflow-x:auto"><table style="width:100%;text-align:left;border-collapse:collapse"><thead><tr><th>Configuration</th><th>Completed</th><th>Mean request time</th><th>Maximum allocated VRAM</th></tr></thead><tbody>'
    for key, name in [("qwen", "Qwen · INT8 · 50 steps · RTX 3090"), ("z", "Z-Image · NF4 · 9 steps · RTX 2070")]:
        values = samples[key]
        duration = f'{sum(v["total_s"] for v in values)/len(values):.1f}s' if values else "Pending"
        peak = f'{max(v["peak_allocated_bytes"] for v in values)/1e9:.2f} GB' if values else "Pending"
        summary += f'<tr><td>{name}</td><td>{len(values)}/10</td><td>{duration}</td><td>{peak}</td></tr>'
    summary += '</tbody></table></div><nav style="display:flex;gap:16px;flex-wrap:wrap;margin-top:20px">'
    summary += "".join(f'<a href="#pair-{p["id"]}">{p["id"]:02d} {html.escape(p["title"])}</a>' for p in prompts)
    summary += "</nav>"
    page = page.replace("</header>", summary + "</header>")
    temp.write_text(page.replace("COMPLETE_COUNT", str(complete)).replace("ROWS", "".join(rows)))
    temp.replace(dest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--endpoint", default="http://100.64.0.18:8211")
    parser.add_argument("--model", choices=["qwen", "z"], required=True)
    args = parser.parse_args()
    prompts = json.loads(Path(args.prompts).read_text())
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    token = Path(args.token_file).read_text().strip()
    render(prompts, out)
    for p in prompts:
        stem = f"{p['id']:02d}-{p['slug']}-{args.model}"
        if (out/(stem+".json")).exists():
            continue
        body = {k: p[k] for k in ["prompt", "seed", "width", "height"]}
        body["harmony_requires"] = {"params_b>=": 20} if args.model == "qwen" else {"params_b<=": 6}
        req = urllib.request.Request(args.endpoint + "/v1/images/generations", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        print(f"START {args.model} pair {p['id']}: {p['title']}", flush=True)
        with urllib.request.urlopen(req, timeout=3600) as response:
            result = json.load(response)
        actual = result["harmony"]
        expected = "qwen_image_2512" if args.model == "qwen" else "z_image_turbo"
        if actual["unit"] != expected or actual["prompt"] != body["prompt"] or actual["seed"] != body["seed"]:
            raise RuntimeError("result provenance differs from the paired request")
        raw = base64.b64decode(result["data"][0]["b64_json"], validate=True)
        im = Image.open(io.BytesIO(raw)); im.load()
        if im.size != (p["width"], p["height"]) or max(ImageStat.Stat(im.convert("RGB")).stddev) < 2:
            raise RuntimeError("image is incorrectly sized or nearly blank")
        (out/(stem+".png")).write_bytes(raw)
        evidence = {"request": body, "harmony": actual, "png_sha256": hashlib.sha256(raw).hexdigest(), "png_bytes": len(raw)}
        (out/(stem+".json")).write_text(json.dumps(evidence, indent=2, ensure_ascii=False))
        render(prompts, out)
        print(f"DONE {args.model} pair {p['id']} in {actual['total_s']}s; peak {actual['peak_allocated_bytes']/1e9:.2f}GB", flush=True)


if __name__ == "__main__":
    main()
