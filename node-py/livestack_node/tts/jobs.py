"""Durable managed speech jobs for a host-owned multi-engine provider.

Cancel is acknowledged immediately but terminal cancellation waits for the real
GPU call and synchronization, including already running acoustic decoding. No
executor-future/socket-close cancellation is reported as physical settlement.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from livestack_node.client import admit


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def install_managed_tts_jobs(app, config, *, manager, coordinator, gpu, busy, validate, prepare, generate, capabilities, synchronize):
    root = Path(config["stateRoot"])
    root.mkdir(parents=True, exist_ok=True)
    owner_fd = os.open(root / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = Path(config["tokenFile"]).read_text().strip()
    if len(token) < 32:
        raise ValueError("Private installed node credential absent")
    app.state.tts_owner_fd = owner_fd
    stop = threading.Event()
    pending = queue.Queue(maxsize=16)
    events = {}
    lock = threading.RLock()
    incarnation = {"pid": os.getpid(), "startTicks": Path("/proc/self/stat").read_text().split(") ", 1)[1].split()[19],
        "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    db_path = root / "jobs.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,digest TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)")
        if db.execute("SELECT id FROM jobs WHERE state NOT IN ('succeeded','failed','cancelled')").fetchone():
            raise RuntimeError("Nonterminal retained work requires old-incarnation reconciliation")

    def authenticated(authorization):
        if not secrets.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "Installed tts-node credential required")

    def row(job_id):
        with sqlite3.connect(db_path) as db:
            item = db.execute("SELECT body,state,receipt FROM jobs WHERE id=?", (job_id,)).fetchone()
        if item is None:
            raise HTTPException(404, "Unknown speech job")
        return json.loads(item[0]), item[1], json.loads(item[2])

    def set_state(job_id, state, receipt):
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE jobs SET state=?,receipt=? WHERE id=?", (state, json.dumps(receipt), job_id))

    def work():
        while not stop.is_set():
            try:
                job_id = pending.get(timeout=.5)
            except queue.Empty:
                continue
            body, state, receipt = row(job_id)
            event = events[job_id]
            path = root / job_id
            lease_id = None
            admitted = False
            began = time.monotonic()
            with busy:
                try:
                    if event.is_set():
                        state = "cancelled"
                        receipt.update(outcome="cancelled", neverAdmitted=True, modelCalls=0)
                        set_state(job_id, "cancelled", receipt)
                        continue
                    set_state(job_id, "admitting", receipt)
                    lease = coordinator.acquire_lease(body["engine"], body["ownerId"], ttl_seconds=7200)
                    lease_id = getattr(lease, "lease_id", None) if not isinstance(lease, dict) else lease.get("lease_id")
                    if not lease_id:
                        raise RuntimeError("Actual local unit lease missing")
                    receipt["localLeaseId"] = lease_id
                    while True:
                        if event.is_set():
                            raise InterruptedError("Cancelled while waiting for real local capacity")
                        grant = admit(body["engine"], owner_id=body["ownerId"], brokers=[config["brokerUrl"]], timeout=240)
                        receipt["localGrant"] = grant
                        if grant.get("granted") or grant.get("degraded"):
                            break
                        receipt["capacityRefusalCount"] = receipt.get("capacityRefusalCount", 0) + 1
                        set_state(job_id, "waiting_capacity", receipt)
                        atomic_json(path / "receipt.json", receipt)
                        # This retries only an explicit pre-execution refusal,
                        # never an unknown submission, generation or warm ack.
                        if event.wait(30):
                            raise InterruptedError("Cancelled while awaiting actual capacity")
                    if grant.get("degraded") or grant.get("device_id") != config["deviceId"] or grant.get("kind") != body["engine"]:
                        raise RuntimeError("Concrete exact local Harmony admission refused")
                    admitted = True
                    if event.is_set():
                        raise InterruptedError("Cancelled after admission, before speech generation")
                    set_state(job_id, "running", receipt)
                    native = gpu(lambda: manager.run(body["engine"], lambda pipe: generate(pipe, body, path, event)))
                    receipt.update(outcome="succeeded", nativeResult=native)
                    state = "succeeded"
                except InterruptedError as error:
                    state = "cancelled"
                    receipt.update(outcome=state, reason=str(error))
                except Exception as error:
                    state = "failed"
                    receipt.update(outcome=state, errorType=type(error).__name__, reason=str(error)[:2000])
                finally:
                    # No terminal publication or lease release until real GPU settlement.
                    if admitted:
                        try:
                            gpu(synchronize)
                            receipt["settlement"] = {"kind": "joined-native-gpu-executor-and-cuda-synchronize", "incarnation": incarnation,
                                "physicalSettled": True, "elapsedSeconds": time.monotonic() - began}
                        except Exception as error:
                            state = "held"
                            receipt.update(outcome=state, settlementError=type(error).__name__)
                    if event.is_set() and state == "succeeded":
                        state = "cancelled"
                        receipt.update(outcome=state, reason="Cancellation observed before terminal publication")
                    if lease_id and state != "held":
                        coordinator.release_lease(lease_id)
                        receipt["localLeaseReleased"] = True
                    atomic_json(path / "receipt.json", receipt)
                    set_state(job_id, state, receipt)
                    pending.task_done()

    @app.get("/v1/tts/health")
    def health():
        return {"ok": True, "release": config["release"], "nodeKind": "polytts",
            "incarnation": incarnation, "installation": {"digest": config["installationDigest"]}}

    @app.get("/v1/tts/capabilities")
    def read_capabilities():
        return {"protocolVersion": 2, "provider": "polytts", **capabilities(), "source": config["sourceIdentity"]}

    @app.post("/v1/tts/jobs", status_code=202)
    def submit(body: dict = Body(...), authorization: str | None = Header(None)):
        authenticated(authorization)
        try:
            body = validate(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if not any(body["ownerId"].startswith(prefix) for prefix in config["allowedOwnerPrefixes"]):
            raise HTTPException(403, "Installed owner scope refused")
        try:
            prepare(body)
        except ValueError as error:
            raise HTTPException(403, str(error)) from error
        key = digest(body)
        with lock, sqlite3.connect(db_path) as db:
            prior = db.execute("SELECT digest,state,receipt FROM jobs WHERE id=?", (body["jobId"],)).fetchone()
            if prior:
                if prior[0] != key:
                    raise HTTPException(409, "Known job identity has a different immutable request")
                return {"jobId": body["jobId"], "state": prior[1], "replayed": True, "requestSha256": key}
            if stop.is_set() or pending.full():
                raise HTTPException(503, "TTS worker admission closed or queue full")
            path = root / body["jobId"]
            path.mkdir()
            receipt = {"jobId": body["jobId"], "requestSha256": key, "incarnation": incarnation,
                "source": config["sourceIdentity"], "modelManifestSha256": config["modelManifestSha256"], "configurationSha256": digest(config),
                "acceptedAt": time.time(), "fleetAuthorizationClaimed": False}
            atomic_json(path / "request.json", body)
            atomic_json(path / "receipt.json", receipt)
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (body["jobId"], key, json.dumps(body), "queued", json.dumps(receipt)))
            db.commit()
            events[body["jobId"]] = threading.Event()
            pending.put_nowait(body["jobId"])
        return {"jobId": body["jobId"], "state": "queued", "requestSha256": key}

    @app.get("/v1/tts/jobs/{job_id}")
    def status(job_id: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        body, state, receipt = row(job_id)
        return {"jobId": job_id, "state": state, "requestSha256": digest(body), "receipt": receipt}

    @app.post("/v1/tts/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        body, state, receipt = row(job_id)
        if state in {"succeeded", "failed", "cancelled"}:
            return {"jobId": job_id, "state": state, "alreadyTerminal": True}
        events[job_id].set()
        return {"jobId": job_id, "state": "cancelling", "settled": False}

    @app.get("/v1/tts/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: str, name: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        _, state, _ = row(job_id)
        if state != "succeeded":
            raise HTTPException(409, "Audio is not admitted as a successful result")
        paths = {"audio.wav": "audio.wav", "receipt.json": "receipt.json", "drama3-take.json": "drama3-take.json"}
        if name not in paths:
            raise HTTPException(404, "Unknown retained artifact")
        target = root / job_id / paths[name]
        if not target.is_file():
            raise HTTPException(404, "Retained speech artifact absent")
        return FileResponse(target)

    worker = threading.Thread(target=work, name="tts-journal", daemon=False)
    worker.start()

    @app.on_event("shutdown")
    def shutdown():
        stop.set()
        for event in events.values():
            event.set()
        worker.join()  # Actual accepted GPU work joins; never force release on a timeout.
        while True:
            try:
                job_id = pending.get_nowait()
            except queue.Empty:
                break
            _, state, receipt = row(job_id)
            if state == "queued":
                receipt.update(outcome="cancelled", neverAdmitted=True, modelCalls=0, reason="Worker shutdown closed queued admission")
                set_state(job_id, "cancelled", receipt)
                atomic_json(root / job_id / "receipt.json", receipt)
            pending.task_done()
        os.close(owner_fd)

    return app

