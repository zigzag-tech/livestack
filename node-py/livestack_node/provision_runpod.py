"""provision_runpod.py — RunPod implementation of the :class:`Provisioner` contract.

Rents an on-demand RunPod pod over the REST API (legacy GraphQL *mutations* 403;
the GraphQL *query* is still used for live pricing), waits for sshd, and tears the
pod down idempotently. This is the reference provider extracted from Benchday's
validated ``runpod_train.py`` (2026-06-20, from CN: ~100s + ~$0.03 for a 1.7B
LoRA SFT on a 3090). A second provider (Aliyun ECI) is a sibling of this file.

Zero non-stdlib deps, same as :mod:`provision`.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import List, Optional

from .provision import (
    CapacityError,
    ComputeHandle,
    ComputeSpec,
    Instance,
    Offer,
    ProvisionError,
    Provisioner,
    _ssh,
)

REST = "https://rest.runpod.io/v1/pods"
GQL = "https://api.runpod.io/graphql"
# Default image: torch 2.4.1 CUDA 12.4; workloads build their own venv on top.
DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# Excluded regardless of price: pre-Ampere (no bf16) and MIG/partition SKUs.
_GPU_DENY = ("V100", "MIG", "P100", "P40", "T4")
# Known-good fallback order if the live price query fails or returns nothing usable.
_STATIC_FALLBACK = [
    ("NVIDIA RTX A5000", "COMMUNITY", 0.16), ("NVIDIA RTX A5000", "SECURE", 0.27),
    ("NVIDIA GeForce RTX 3090", "COMMUNITY", 0.22), ("NVIDIA RTX A6000", "SECURE", 0.49),
]


class RunpodProvisioner(Provisioner):
    provider = "runpod"

    def __init__(self, api_key: Optional[str] = None,
                 ssh_pubkey_path: str = "~/.ssh/id_ed25519.pub") -> None:
        super().__init__()
        self._api_key = api_key or os.environ.get("RUNPOD_API_KEY")
        self._pubkey_path = os.path.expanduser(ssh_pubkey_path)

    # -- REST plumbing -------------------------------------------------------
    def _key(self) -> str:
        if not self._api_key:
            raise ProvisionError(
                "set RUNPOD_API_KEY (read-write key; REST is required — legacy GraphQL mutations 403)")
        return self._api_key

    def _req(self, method: str, url: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self._key()}", "content-type": "application/json"})
        # The REST API intermittently resets the connection; retry transient
        # (non-HTTP) errors so one reset doesn't abort a run.
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    txt = r.read().decode()
                    return r.status, (json.loads(txt) if txt.strip() else {})
            except urllib.error.HTTPError as e:
                return e.code, {"error": e.read().decode()[:200]}
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if attempt == 3:
                    return 0, {"error": f"transient: {e}"}
                time.sleep(3 * (attempt + 1))

    # -- offers (live pricing, cheapest-first) -------------------------------
    def offers(self, spec: ComputeSpec) -> List[Offer]:
        try:
            _, d = self._req("POST", GQL, {"query":
                "query{gpuTypes{id memoryInGb communityCloud secureCloud "
                "communityPrice securePrice}}"})
            types = (d.get("data") or {}).get("gpuTypes") or []
        except Exception as e:  # noqa: BLE001
            print(f"[runpod] price query failed ({e}); using static fallback order")
            types = []
        raw: list[tuple[str, str, float]] = []
        for t in types:
            mem = t.get("memoryInGb") or 0
            gid = t.get("id") or ""
            if not (spec.min_vram_gb <= mem <= spec.max_vram_gb) or any(x in gid for x in _GPU_DENY):
                continue
            if spec.gpu_hint and spec.gpu_hint not in gid:
                continue
            if t.get("communityCloud") and t.get("communityPrice"):
                raw.append((gid, "COMMUNITY", float(t["communityPrice"])))
            if t.get("secureCloud") and t.get("securePrice"):
                raw.append((gid, "SECURE", float(t["securePrice"])))
        if not raw:
            raw = list(_STATIC_FALLBACK)
        raw.sort(key=lambda c: c[2])
        return [Offer(provider=self.provider, sku=g, zone=c, price_per_hr=p) for g, c, p in raw]

    # -- acquire / terminate -------------------------------------------------
    def acquire(self, spec: ComputeSpec, offer: Optional[Offer] = None) -> ComputeHandle:
        if offer is None:
            offers = self.offers(spec)
            if not offers:
                raise CapacityError("runpod: no eligible GPU offers")
            offer = offers[0]
        pub = open(self._pubkey_path).read().strip()
        env = {"PUBLIC_KEY": pub, **dict(spec.env)}
        st, r = self._req("POST", REST, {
            "name": spec.name, "imageName": spec.image or DEFAULT_IMAGE,
            "gpuTypeIds": [offer.sku], "gpuCount": 1, "cloudType": offer.zone,
            "containerDiskInGb": 40, "ports": ["22/tcp"], "env": env,
        })
        if not (st == 201 and r.get("id")):
            # Out of stock / rejected: nothing was created, so this is a capacity
            # miss the caller should fall through on — not a teardown-worthy error.
            raise CapacityError(f"{offer}: create -> {st} {str(r.get('error', r))[:70]}")
        pid = r["id"]
        self._live.add(pid)                        # register BEFORE reachable: signal-safe
        print(f"[runpod] created {pid} on {offer.sku}/{offer.zone} (${offer.price_per_hr}/hr)")
        try:
            ip, port = self._wait_ssh(pid)
        except Exception:
            self.release_id(pid)                   # created but unusable -> tear down, then propagate
            raise
        return ComputeHandle(provider=self.provider, instance_id=pid,
                             host=ip, port=port, user="root", offer=offer)

    def _wait_ssh(self, pid: str) -> tuple[str, int]:
        for _ in range(40):
            time.sleep(10)
            _, r = self._req("GET", f"{REST}/{pid}")
            ip = r.get("publicIp")
            port = (r.get("portMappings") or {}).get("22")
            if ip and port:
                # A slow first connect raises TimeoutExpired = "not ready yet";
                # keep polling rather than killing the candidate.
                try:
                    if _ssh(ip, int(port), "root", "echo ok", check=False, timeout=20).returncode == 0:
                        print(f"[runpod] ssh ready root@{ip}:{port}")
                        return ip, int(port)
                except subprocess.SubprocessError:
                    continue
        raise ProvisionError("pod never became SSH-reachable")

    def _terminate(self, instance_id: str) -> None:
        # Retry on HTTP failure (a pod that fails to delete bills silently);
        # 404 = already gone (success).
        for attempt in range(3):
            st, r = self._req("DELETE", f"{REST}/{instance_id}")
            if st in (200, 201, 204, 404):
                print(f"[runpod] terminated pod {instance_id} (HTTP {st})")
                return
            print(f"[runpod] terminate {instance_id} attempt {attempt + 1}/3 -> HTTP {st} {str(r)[:80]}")
            time.sleep(3)
        print(f"[runpod] WARNING: could not confirm termination of {instance_id}; reap will catch it")

    # -- listing (for reap_orphans) ------------------------------------------
    def list_instances(self, name_prefix: str) -> List[Instance]:
        _, r = self._req("GET", REST)
        pods = r if isinstance(r, list) else (r.get("data", []) if isinstance(r, dict) else [])
        out: List[Instance] = []
        for p in pods:
            out.append(Instance(
                id=p.get("id", ""), name=p.get("name") or "",
                age_min=_age_minutes(p.get("createdAt") or ""),
                price_per_hr=p.get("costPerHr")))
        return out


def _age_minutes(created: str) -> float:
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(created.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M:%S.%f %z")
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 0.0  # unknown age -> treat as fresh so a live run is never reaped
