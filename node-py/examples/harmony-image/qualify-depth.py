#!/usr/bin/env python3
"""Request one depth-guided image through the production Harmony image ingress.

This is a client only. It never starts a broker, worker or gateway: depth units
run as Harmony-managed units registered with the host broker, so Harmony can see
their memory, drain other tenants through exclusive-device leases and restore
them afterwards. (An earlier `up` lifecycle started its own unregistered hostd,
worker and gateway; production Harmony could not see that GPU memory, and
polytts/polyasr/LLM tenants were refused for 15+ minutes. Do not bring it back.)

    qualify-depth.py --request req.json [--guide depth.png] --out out.png
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

INGRESS = "http://100.64.0.18:8211"
TOKEN_FILE = "/home/ubuntu/harmony-image/worker.token"


class QualificationError(RuntimeError):
    pass


def attach_guide(request: dict, guide: Path) -> dict:
    data = guide.read_bytes()
    return {**request, "control_image": {
        "media_type": "image/png",
        "b64_json": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }}


def verify(request: dict, response: dict) -> bytes:
    """Check the PNG and the evidence the ingress returns against the request."""
    from PIL import Image
    try:
        png = base64.b64decode(response["data"][0]["b64_json"], validate=True)
        with Image.open(io.BytesIO(png)) as image:
            size = image.size
            extrema = image.convert("L").getextrema()
    except Exception as exc:
        raise QualificationError("response PNG cannot be decoded") from exc
    if size != (request["width"], request["height"]):
        raise QualificationError("PNG dimensions do not match the request")
    if extrema[0] == extrema[1]:
        raise QualificationError("PNG is blank")
    harmony = response.get("harmony")
    if not isinstance(harmony, dict):
        raise QualificationError("response has no harmony evidence")
    requires = request["harmony_requires"]
    for key in ("model", "revision", "quantization"):
        if harmony.get(key) != requires[key]:
            raise QualificationError(f"harmony evidence {key} does not match the request")
    depth = harmony.get("depth_guide")
    expected = {
        "image_sha256": request["control_image"]["sha256"],
        "conditioning_scale": request["controlnet_conditioning_scale"],
        "controlnet_model": requires["controlnet_model"],
        "controlnet_revision": requires["controlnet_revision"],
        "controlnet_config_sha256": requires["controlnet_config_sha256"],
    }
    if not isinstance(depth, dict) or any(depth.get(k) != v for k, v in expected.items()):
        raise QualificationError("depth-guide evidence does not match the request")
    for name in ("fleet_grant", "local_grant"):
        grant = harmony.get(name)
        if not isinstance(grant, dict) or grant.get("granted") is not True or grant.get("degraded"):
            raise QualificationError(f"{name} was not a concrete Harmony grant")
    leases = harmony.get("device_leases")
    grants = harmony.get("device_grants")
    if (not isinstance(leases, list) or not leases or not isinstance(grants, list)
            or [g.get("lease_id") for g in grants] != [l.get("lease_id") for l in leases]
            or any(g.get("state") != "exclusive" for g in grants)):
        raise QualificationError("depth units must run under Harmony exclusive-device leases")
    return png


def post(ingress: str, token: str, request: dict, timeout: float) -> dict:
    http = urllib.request.Request(
        ingress.rstrip("/") + "/v1/images/generations", data=json.dumps(request).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(http, timeout=timeout) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as exc:
        raise QualificationError(f"ingress HTTP {exc.code}: {exc.read().decode()[:500]}") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--guide", type=Path, help="depth PNG; replaces the request's control_image")
    parser.add_argument("--out", required=True, type=Path, help="PNG path; the receipt is written beside it")
    parser.add_argument("--ingress", default=INGRESS)
    parser.add_argument("--token-file", default=TOKEN_FILE, type=Path)
    parser.add_argument("--timeout", default=7200, type=float)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text())
        if args.guide:
            request = attach_guide(request, args.guide)
        response = post(args.ingress, args.token_file.read_text().strip(), request, args.timeout)
        png = verify(request, response)
    except (QualificationError, OSError, ValueError, KeyError) as exc:
        print(f"qualify-depth: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(png)
    receipt = {"request": {k: v for k, v in request.items() if k != "control_image"},
               "control_image_sha256": request["control_image"]["sha256"],
               "ingress": args.ingress, "output": str(args.out),
               "output_sha256": hashlib.sha256(png).hexdigest(), "harmony": response["harmony"]}
    receipt_path = args.out.with_suffix(".receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
