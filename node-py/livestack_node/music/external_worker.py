"""Bounded paid music jobs behind Harmony; remote execution is never GPU residency.

Only a new durable job can submit. Ambiguous submission remains held, and a
restart never resubmits it. Remote results/status are retained without secrets.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from livestack_node import ManagedUnit, attach, counting

MODELS = {
    "eleven-music": "fal-ai/elevenlabs/music",
    "stable-audio-3-medium": "fal-ai/stable-audio-3/medium/text-to-audio",
}
# Conservative retained ceilings for the 24-second audition lane, not invoices.
CEILINGS = {"eleven-music": 0.60, "stable-audio-3-medium": 0.10}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_request(body):
    if not isinstance(body, dict) or set(body) != {"jobId", "ownerId", "model", "prompt", "seconds", "seed"}:
        raise ValueError("Exact external music request required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(body["jobId"])):
        raise ValueError("Invalid job identity")
    if body["ownerId"] != "histo:music-theme-comparison:20261024" or body["model"] not in MODELS:
        raise ValueError("Owner or model outside this audition lane")
    if type(body["seconds"]) is not int or not 3 <= body["seconds"] <= 24:
        raise ValueError("Bounded short audition required")
    if not isinstance(body["prompt"], str) or not 20 <= len(body["prompt"]) <= 1500:
        raise ValueError("Bounded prompt required")
    if type(body["seed"]) is not int or not 0 <= body["seed"] < 2**32:
        raise ValueError("Explicit seed required")
    return body


def create_app(config):
    root = Path(config["stateRoot"])
    root.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock_fd = os.open(root / ".owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = Path(config["tokenFile"]).read_text().strip()
    api_key = Path(config["apiKeyFile"]).read_text().strip()
    if min(len(token), len(api_key)) < 32 or Path(config["apiKeyFile"]).stat().st_mode & 0o077:
        raise ValueError("Private credentials required")
    db_path, lock, pending = root / "jobs.sqlite3", threading.RLock(), queue.Queue()
    busy, closing = counting(), threading.Event()
    incarnation = {"pid": os.getpid(), "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,digest TEXT,body TEXT,state TEXT,receipt TEXT,cost REAL)")
        db.execute("UPDATE jobs SET state='held' WHERE state NOT IN ('succeeded','failed','held')")

    app = FastAPI(title="Harmony bounded external music audition provider")
    unit = ManagedUnit("external_music_control", lambda: True, lambda: None, footprint=16 * 1024**2,
                       attributes={"class": "music", "execution": "remote-paid", "models": list(MODELS)})
    manager, coordinator = attach(app, host_id="zz-tower2-music-audition", kind="music_external",
        units={unit.name: unit}, port=config["port"], in_flight=busy, backend="cpu",
        idle_seconds=120, coload=True, gpu_call=lambda fn: fn(),
        inventory={"models": MODELS, "execution": "remote-paid", "max_seconds": 24,
                   "budget_usd": config["budgetUsd"], "gpu_residency_claimed": False})

    def authenticate(value):
        if not secrets.compare_digest(value or "", "Bearer " + token):
            raise HTTPException(401, "Private Harmony credential required")

    def row(job):
        with sqlite3.connect(db_path) as db:
            value = db.execute("SELECT digest,body,state,receipt FROM jobs WHERE id=?", (job,)).fetchone()
        if value is None:
            raise HTTPException(404, "Unknown owned job")
        return value[0], json.loads(value[1]), value[2], json.loads(value[3])

    def store(job, state, receipt):
        temporary = root / job / "receipt.tmp"
        temporary.write_bytes(json.dumps(receipt, indent=2).encode())
        temporary.replace(root / job / "receipt.json")
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE jobs SET state=?,receipt=? WHERE id=?", (state, json.dumps(receipt), job))

    def remote(method, url, body=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "queue.fal.run" or parsed.username or parsed.password:
            raise ValueError("Unexpected paid provider control URL")
        req = urllib.request.Request(url, data=None if body is None else canonical(body), method=method,
             headers={"Authorization": "Key " + api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read(2 * 1024**2))

    def run(job):
        key, body, _, receipt = row(job)
        lease = coordinator.acquire_lease(unit.name, body["ownerId"], ttl_seconds=3600)
        lease_id = lease.get("lease_id") if isinstance(lease, dict) else lease.lease_id
        receipt.update(localControlLeaseId=lease_id)
        try:
            store(job, "submitting", receipt)
            model = MODELS[body["model"]]
            payload = {"prompt": body["prompt"]}
            if body["model"] == "eleven-music":
                payload.update(music_length_ms=body["seconds"] * 1000, force_instrumental=True,
                               output_format="mp3_44100_128")
            else:
                payload.update(duration=body["seconds"], seed=body["seed"], enable_prompt_expansion=False, output_format="mp3")
            (root / job / "provider-input.json").write_bytes(canonical(payload))
            receipt["submissionMayHaveOccurred"] = True
            store(job, "submitting", receipt)
            accepted = remote("POST", "https://queue.fal.run/" + model, payload)
            if not isinstance(accepted.get("request_id"), str):
                raise ValueError("Missing accepted remote request identity")
            receipt.update(remoteRequestId=accepted["request_id"], accepted=accepted)
            store(job, "running", receipt)
            deadline = time.monotonic() + 1800
            while True:
                status = remote("GET", accepted["status_url"])
                receipt["remoteStatus"] = status
                store(job, "running", receipt)
                if status.get("status") == "COMPLETED":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Remote job remains observable, never resubmit")
                time.sleep(3)
            result = remote("GET", accepted["response_url"])
            audio = result["audio"]
            parsed = urllib.parse.urlsplit(audio["url"])
            if parsed.scheme != "https" or parsed.username or parsed.password or not (
                parsed.hostname.endswith(".fal.media") or parsed.hostname == "storage.googleapis.com"
            ):
                raise ValueError("Unexpected provider artifact URL")
            with urllib.request.urlopen(audio["url"], timeout=20) as response:
                raw = response.read(32 * 1024**2 + 1)
            if not 0 < len(raw) <= 32 * 1024**2:
                raise ValueError("Artifact outside bounds")
            (root / job / "audio.mp3").write_bytes(raw)
            receipt.update(result=result, artifact={"name": "audio.mp3", "sha256": sha(raw), "bytes": len(raw)},
                remoteTerminalObserved=True, remotePhysicalSettlementClaimed=False,
                gpuResidencyClaimed=False, localControlSettled=True)
            store(job, "succeeded", receipt)
        except Exception as error:
            # No credentials, HTTP bodies or arbitrary remote errors cross this boundary.
            receipt.update(errorType=type(error).__name__, errorCode=getattr(error, "code", None),
                           automaticResubmit=False, localControlSettled=True)
            store(job, "held", receipt)
        finally:
            coordinator.release_lease(lease_id)
            receipt["localControlLeaseReleased"] = True
            _, _, state, _ = row(job)
            store(job, state, receipt)

    def work():
        while not closing.is_set():
            try:
                job = pending.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                with busy:
                    manager.run(unit.name, lambda _: run(job))
            finally:
                pending.task_done()

    worker = threading.Thread(target=work, name="Harmony-external-music", daemon=False)
    worker.start()

    @app.get("/health")
    def health():
        return {"ok": worker.is_alive() and not closing.is_set(), "kind": "music_external",
                "incarnation": incarnation, "sourceSha256": sha(Path(__file__).read_bytes())}

    @app.post("/v1/music/jobs", status_code=202)
    def submit(body: dict = Body(...), authorization: str | None = Header(None)):
        authenticate(authorization)
        try:
            validate_request(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        key, job = sha(canonical(body)), body["jobId"]
        with lock, sqlite3.connect(db_path) as db:
            prior = db.execute("SELECT digest,state FROM jobs WHERE id=?", (job,)).fetchone()
            if prior:
                if prior[0] != key:
                    raise HTTPException(409, "Changed immutable job request")
                return {"jobId": job, "state": prior[1], "replayed": True, "requestSha256": key}
            spend = db.execute("SELECT COALESCE(SUM(cost),0) FROM jobs").fetchone()[0]
            if closing.is_set() or spend + CEILINGS[body["model"]] > config["budgetUsd"] + 1e-9:
                raise HTTPException(403, "Audition budget or admission closed")
            path = root / job
            path.mkdir()
            (path / "request.json").write_bytes(canonical(body))
            receipt = {"jobId": job, "requestSha256": key, "sourceSha256": sha(Path(__file__).read_bytes()),
                "incarnation": incarnation, "route": "Harmony", "execution": "remote-paid",
                "costCeilingUsd": CEILINGS[body["model"]], "automaticResubmit": False}
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)", (job, key, json.dumps(body), "queued",
                json.dumps(receipt), CEILINGS[body["model"]]))
        pending.put(job)
        return {"jobId": job, "state": "queued", "requestSha256": key}

    @app.get("/v1/music/jobs/{job}")
    def status(job: str, authorization: str | None = Header(None)):
        authenticate(authorization)
        key, _, state, receipt = row(job)
        return {"jobId": job, "state": state, "requestSha256": key, "receipt": receipt}

    @app.get("/v1/music/jobs/{job}/audio")
    def artifact(job: str, authorization: str | None = Header(None)):
        authenticate(authorization)
        if row(job)[2] != "succeeded":
            raise HTTPException(409, "No accepted result")
        return FileResponse(root / job / "audio.mp3", media_type="audio/mpeg")

    @app.on_event("shutdown")
    def shutdown():
        closing.set()
        worker.join()
        os.close(lock_fd)

    return app


if __name__ == "__main__":
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_EXTERNAL_MUSIC_CONFIG"]).read_text())
    if not isinstance(config["budgetUsd"], (int, float)) or not math.isfinite(config["budgetUsd"]) or not 0 < config["budgetUsd"] <= 3:
        raise ValueError("Explicit small audition budget required")
    uvicorn.run(create_app(config), host="127.0.0.1", port=config["port"])
