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
import stat
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


def read_owned_speech_take(config, current, reference):
    """Resolve exact completed history; never trust caller-supplied voice codes."""
    job_id = reference["jobId"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", job_id) or job_id == current["jobId"]:
        raise ValueError("Invalid retained history job identity")
    root = Path(config["stateRoot"])
    with sqlite3.connect((root / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        row = db.execute("SELECT body,digest,state,receipt FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row[2] != "succeeded" or row[1] != reference["requestSha256"]:
        raise ValueError("Exact succeeded history request refused")
    body, receipt = json.loads(row[0]), json.loads(row[3])
    if digest(body) != row[1]:
        raise ValueError("Retained history body digest changed")
    if any(body.get(key) != current.get(key) for key in ("ownerId", "engine", "voiceMode", "voiceId", "referenceSha256")):
        raise ValueError("History belongs to another owner/engine/reference")
    if receipt.get("source") != config["sourceIdentity"] or receipt.get("settlement", {}).get("physicalSettled") is not True or receipt.get("localLeaseReleased") is not True:
        raise ValueError("History producer/physical settlement refused")
    native = receipt.get("nativeResult", {})
    unconditioned = current.get("voiceMode") == "unconditioned"
    expected_reference_text = None if unconditioned else config["voices"][current["voiceId"]].get("referenceTextSha256")
    if unconditioned and native.get("voiceMode") != "unconditioned":
        raise ValueError("Unconditioned history voice mode differs")
    if native.get("referenceTextSha256") != expected_reference_text:
        raise ValueError("History reference transcript profile differs")
    descriptors = {item["name"]: item for item in native.get("artifacts", [])}
    if descriptors.get("drama3-take.json", {}).get("sha256") != reference["takeSha256"]:
        raise ValueError("Exact history artifact descriptor refused")
    retained = {}
    for name in ("drama3-take.json", "drama3-generation.json"):
        descriptor = descriptors.get(name)
        if not descriptor or not 0 < descriptor["bytes"] <= 4*1024**2:
            raise ValueError("Bounded retained native history artifact required")
        fd = os.open(root / job_id / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Native history artifact is not a regular file")
            payload = stream.read(4*1024**2 + 1)
        if len(payload) != descriptor["bytes"] or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
            raise ValueError("Retained history bytes changed")
        retained[name] = json.loads(payload)
    if retained["drama3-generation.json"].get("reached_limit") is not False:
        raise ValueError("Incomplete native history refused")
    take = retained["drama3-take.json"]
    if set(take) != {"text", "vq_codes"} or take["text"] != body["text"]:
        raise ValueError("Native history text/source identity refused")
    return take, {"jobId": job_id, "requestSha256": row[1], "takeSha256": reference["takeSha256"]}


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
            admission_unknown = False
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
                        try:
                            grant = admit(body["engine"], owner_id=body["ownerId"], brokers=[config["brokerUrl"]], timeout=240)
                        except Exception:
                            # A lost admission/warm response is not proof that
                            # the broker's native warm handler never executed.
                            admission_unknown = True
                            raise
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
                    diagnostics = getattr(error, "native_failure", None)
                    if diagnostics is not None:
                        encoded = json.dumps(diagnostics, ensure_ascii=False)
                        if len(encoded.encode()) <= 65536:
                            receipt["nativeFailure"] = diagnostics
                finally:
                    # No terminal publication or lease release until real GPU settlement.
                    if admission_unknown:
                        state = "held"
                        receipt.update(outcome=state, admissionOutcome="unknown", physicalSettlementQualified=False)
                        stop.set()
                    # Also join accepted warm work on the owned executor when
                    # admission fails; no lease is released for an unknown ack.
                    try:
                        gpu(synchronize)
                        receipt["settlement"] = {"kind": "joined-native-gpu-executor-and-cuda-synchronize", "incarnation": incarnation,
                            "physicalSettled": not admission_unknown, "executorBarrierPassed": True, "elapsedSeconds": time.monotonic() - began}
                    except Exception as error:
                        state = "held"
                        receipt.update(outcome=state, settlementError=type(error).__name__)
                    if state == "held":
                        stop.set()
                    if event.is_set() and state == "succeeded":
                        state = "cancelled"
                        receipt.update(outcome=state, reason="Cancellation observed before terminal publication")
                    if lease_id and state != "held":
                        try:
                            coordinator.release_lease(lease_id)
                            receipt["localLeaseReleased"] = True
                        except Exception as error:
                            state = "held"
                            stop.set()
                            receipt.update(outcome=state, leaseReleaseOutcome="unknown", leaseReleaseError=type(error).__name__)
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
        if state == "held":
            raise HTTPException(409, "Exact retained admission/settlement requires owner reconciliation")
        events[job_id].set()
        return {"jobId": job_id, "state": "cancelling", "settled": False}

    @app.get("/v1/tts/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: str, name: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        _, state, _ = row(job_id)
        if state != "succeeded":
            raise HTTPException(409, "Audio is not admitted as a successful result")
        paths = {"audio.wav": "audio.wav", "receipt.json": "receipt.json", "drama3-take.json": "drama3-take.json", "drama3-generation.json": "drama3-generation.json"}
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

