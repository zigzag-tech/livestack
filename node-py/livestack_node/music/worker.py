"""Durable YuE2 instrumental jobs on one actually admitted Harmony device.

Cancel is acknowledged immediately but terminal cancellation waits for the real
GPU call and synchronization, including an already running VAE decode. No
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

from livestack_node import attach, counting
from livestack_node.client import admit
from livestack_node.manager import ManagedUnit, ResidencyPolicy


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


def validate_request(body):
    required = {"jobId", "ownerId", "style", "sectionTags", "seed"}
    if not isinstance(body, dict) or not required <= body.keys() or body.keys() - required - {"scoreAbc", "lyricsMode"}:
        raise ValueError("Expected jobId, ownerId, style, sectionTags, seed and optional scoreAbc/lyricsMode")
    if not isinstance(body["jobId"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", body["jobId"]):
        raise ValueError("Invalid jobId")
    if not isinstance(body["ownerId"], str) or not body["ownerId"].startswith("histo:") or len(body["ownerId"]) > 192:
        raise ValueError("This installed node accepts the explicitly provisioned histo owner prefix")
    if not isinstance(body["style"], str) or not 1 <= len(body["style"]) <= 8000:
        raise ValueError("Invalid musical style")
    if any(word not in body["style"].lower() for word in ["instrumental", "no vocals", "no singing", "no choir"]):
        raise ValueError("Explicit instrumental/no vocals/no singing/no choir conditions required")
    if type(body["seed"]) is not int or not 0 <= body["seed"] < 2**63:
        raise ValueError("Invalid seed")
    tags = body["sectionTags"]
    if not isinstance(tags, list) or not 1 <= len(tags) <= 12 or any(t not in {"Intro", "Verse", "Chorus", "Bridge", "Outro", "Solo", "Instrumental"} for t in tags):
        raise ValueError("Invalid instrumental section tags")
    if "scoreAbc" in body and (not isinstance(body["scoreAbc"], str) or not 1 <= len(body["scoreAbc"]) <= 100000):
        raise ValueError("Invalid exact editable score")
    if not isinstance(body.get("lyricsMode", "sections"), str) or body.get("lyricsMode", "sections") not in {"empty", "sections"}:
        raise ValueError("lyricsMode must explicitly select empty or sections")
    return dict(body)


def create_app(config):
    root = Path(config["stateRoot"])
    root.mkdir(parents=True, exist_ok=True)
    owner_fd = os.open(root / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = Path(config["tokenFile"]).read_text().strip()
    if len(token) < 32:
        raise ValueError("Private installed node credential is absent")
    source_root = Path(config["sourceRoot"])
    sys.path.insert(0, str(source_root / "skills/yue2-music/instrumental/scripts"))
    from instrumental import validate_score, lyric_tags
    from instrumentalize import convert_score
    from yue2 import YuE2Pipeline
    from yue2.pipeline import SongResult
    from yue2.protocol import SongRequest
    from yue2.storage import identity
    import numpy as np
    import soundfile as sf
    import torch
    from concurrent.futures import ThreadPoolExecutor

    actual_uuid = str(torch.cuda.get_device_properties(0).uuid)
    if actual_uuid.removeprefix("GPU-") != config["cudaVisibleDevice"].removeprefix("GPU-"):
        raise ValueError("Actual CUDA UUID differs from the installed physical device")

    # All CUDA work, including warm/evict, uses the same actual GPU thread.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-gpu")
    holder = {}
    stop = threading.Event()
    pending = queue.Queue(maxsize=16)
    events = {}
    lock = threading.RLock()
    busy = counting()
    process_ticks = Path("/proc/self/stat").read_text().split(") ", 1)[1].split()[19]
    incarnation = {"pid": os.getpid(), "startTicks": process_ticks, "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    db_path = root / "jobs.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,digest TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)")
        uncertain = db.execute("SELECT id,receipt FROM jobs WHERE state NOT IN ('succeeded','failed','cancelled')").fetchall()
    if uncertain:
        raise RuntimeError("Retained nonterminal jobs require exact old-incarnation reconciliation; refusing blind startup")

    def gpu(fn):
        return executor.submit(fn).result()

    def load():
        pipe = YuE2Pipeline.from_pretrained(config["modelPath"], vae=config["vaePath"],
            local_files_only=True, device="cuda:0", backend="torch", memory_budget_gib=config["memoryBudgetGiB"],
            progress=False, verify_hashes=True)
        try:
            pipe._load_model()
            torch.cuda.synchronize()
        except BaseException:
            pipe.close()
            torch.cuda.synchronize()
            raise
        holder["pipe"] = pipe
        return pipe

    def free():
        pipe = holder.pop("pipe", None)
        if pipe is not None:
            torch.cuda.synchronize()
            pipe.close()
            torch.cuda.synchronize()

    attrs = {"class": "music", "task": "text_to_music", "instrumental": True, "params_b": 3,
        "model": "m-a-p/YuE2-3B", "revision": config["modelRevision"], "sample_rate": 48000,
        "channels": 2, "license": "cc-by-nc-4.0-plus-individual-creator-permission", "max_concurrent": 1}
    unit = ManagedUnit(config["unit"], load, free, footprint=config["residencyBudgetBytes"],
        residency_policy=ResidencyPolicy.UNPINNED, min_resident=0, attributes=attrs,
        spread_group="music", priority=30, reload_cost=120, min_residency_s=60)
    app = FastAPI(title="Harmony YuE2 instrumental jobs")
    app.state.owner_fd = owner_fd
    manager, coordinator = attach(app, host_id=config["hostId"], kind="music", units={config["unit"]: unit},
        idle_seconds=120, coload=True, gpu_call=gpu, port=config["port"], in_flight=busy,
        device_id=config["deviceId"], inventory=attrs)

    def authenticated(authorization):
        if not secrets.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "Installed music-node credential required")

    def row(job_id):
        with sqlite3.connect(db_path) as db:
            item = db.execute("SELECT body,state,receipt FROM jobs WHERE id=?", (job_id,)).fetchone()
        if item is None:
            raise HTTPException(404, "Unknown music job")
        return json.loads(item[0]), item[1], json.loads(item[2])

    def set_state(job_id, state, receipt):
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE jobs SET state=?,receipt=? WHERE id=?", (state, json.dumps(receipt), job_id))

    def generate(pipe, body, path, cancel):
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        style = body["style"]
        lyrics = "\n\n".join("[" + x + "]" for x in body["sectionTags"]) + "\n"
        if "scoreAbc" in body:
            abc = body["scoreAbc"]
            score = validate_score(abc)
            composition = {"composer": "explicit-owner-score", "inputSha256": hashlib.sha256(abc.encode()).hexdigest()}
        else:
            original = pipe.plan(request=SongRequest(id=body["jobId"], style=style, lyrics=lyrics, cot="full", seed=body["seed"]), cancelled=cancel.is_set)
            original.save(path / "original-plan")
            if original.truncated or not original.abc:
                raise ValueError("The untouched model plan is empty or truncated")
            abc, conversion = convert_score(original.abc)
            score = validate_score(abc)
            composition = {"composer": "YuE2", "instrumentalConversion": conversion}
        (path / "instrumental.abc").write_text(abc)
        atomic_json(path / "composition.json", composition)
        # Required instrumental semantics are explicit, never hidden lyrics.
        request = SongRequest(id=body["jobId"], style=style,
            lyrics="" if body.get("lyricsMode", "sections") == "empty" else lyric_tags(abc), abc=abc,
            cot="full" if score.voices["Vocal"].chords else "melody", seed=body["seed"])
        plan = pipe.plan(request=request, cancelled=cancel.is_set)
        semantic = pipe.generate_semantic(plan, cancelled=cancel.is_set)
        latents = pipe.synthesize(semantic, cancelled=cancel.is_set)
        if cancel.is_set():
            raise InterruptedError("Cancellation observed before VAE decode")
        audio = pipe.decode(latents)
        torch.cuda.synchronize()
        if cancel.is_set():
            raise InterruptedError("Cancellation joined the already running VAE decode")
        if audio.ndim != 2 or audio.shape[1] != 2 or not np.isfinite(audio).all() or len(audio) <= 0:
            raise ValueError("Invalid native stereo audio")
        effective = pipe.effective_config(request)
        native_identity = identity({"request": request.to_dict(), "config": effective, "weights": pipe.weights})
        result = SongResult(audio, 48000, semantic, latents, effective, pipe.weights,
            {"e2e_seconds": time.monotonic() - started}, native_identity)
        native = result.save_artifacts(path / "native")
        if any(native["truncated"].values()):
            raise ValueError("Music generation truncated; retained for review, not successful delivery")
        native.update(measurement={"torchPeakAllocatedBytes": torch.cuda.max_memory_allocated(),
            "torchPeakReservedBytes": torch.cuda.max_memory_reserved(), "configuredResidencyBudgetBytes": config["residencyBudgetBytes"],
            "budgetSource": "explicit conservative allocation budget; measured actual peaks separate"},
            instrumentalListeningChecked=False)
        return native

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
                    lease = coordinator.acquire_lease(config["unit"], body["ownerId"], ttl_seconds=7200)
                    lease_id = getattr(lease, "lease_id", None) if not isinstance(lease, dict) else lease.get("lease_id")
                    if not lease_id:
                        raise RuntimeError("Actual local unit lease missing")
                    receipt["localLeaseId"] = lease_id
                    while True:
                        if event.is_set():
                            raise InterruptedError("Cancelled while waiting for real local capacity")
                        grant = admit(config["unit"], owner_id=body["ownerId"], brokers=[config["brokerUrl"]], timeout=240)
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
                    if grant.get("degraded") or grant.get("device_id") != config["deviceId"] or grant.get("kind") != config["unit"]:
                        raise RuntimeError("Concrete exact local Harmony admission refused")
                    admitted = True
                    if event.is_set():
                        raise InterruptedError("Cancelled after admission, before music generation")
                    set_state(job_id, "running", receipt)
                    native = gpu(lambda: manager.run(config["unit"], lambda pipe: generate(pipe, body, path, event)))
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
                    if gpu(torch.cuda.is_initialized):
                        try:
                            gpu(lambda: torch.cuda.synchronize())
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

    @app.get("/health")
    def health():
        return {"ok": True, "release": config["release"], "nodeKind": "music", "loaded": unit.loaded,
            "incarnation": incarnation, "installation": {"digest": config["installationDigest"]}}

    @app.post("/v1/music/jobs", status_code=202)
    def submit(body: dict = Body(...), authorization: str | None = Header(None)):
        authenticated(authorization)
        try:
            body = validate_request(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        key = digest(body)
        with lock, sqlite3.connect(db_path) as db:
            prior = db.execute("SELECT digest,state,receipt FROM jobs WHERE id=?", (body["jobId"],)).fetchone()
            if prior:
                if prior[0] != key:
                    raise HTTPException(409, "Known job identity has a different immutable request")
                return {"jobId": body["jobId"], "state": prior[1], "replayed": True, "requestSha256": key}
            if stop.is_set() or pending.full():
                raise HTTPException(503, "Music worker admission closed or queue full")
            path = root / body["jobId"]
            path.mkdir()
            receipt = {"jobId": body["jobId"], "requestSha256": key, "incarnation": incarnation,
                "source": config["sourceIdentity"], "configurationSha256": digest(config),
                "acceptedAt": time.time(), "fleetAuthorizationClaimed": False}
            atomic_json(path / "request.json", body)
            atomic_json(path / "receipt.json", receipt)
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (body["jobId"], key, json.dumps(body), "queued", json.dumps(receipt)))
            events[body["jobId"]] = threading.Event()
        pending.put_nowait(body["jobId"])
        return {"jobId": body["jobId"], "state": "queued", "requestSha256": key}

    @app.get("/v1/music/jobs/{job_id}")
    def status(job_id: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        body, state, receipt = row(job_id)
        return {"jobId": job_id, "state": state, "requestSha256": digest(body), "receipt": receipt}

    @app.post("/v1/music/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        body, state, receipt = row(job_id)
        if state in {"succeeded", "failed", "cancelled"}:
            return {"jobId": job_id, "state": state, "alreadyTerminal": True}
        events[job_id].set()
        return {"jobId": job_id, "state": "cancelling", "settled": False}

    @app.get("/v1/music/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: str, name: str, authorization: str | None = Header(None)):
        authenticated(authorization)
        _, state, _ = row(job_id)
        if state != "succeeded":
            raise HTTPException(409, "Audio is not admitted as a successful result")
        paths = {"audio.flac": "native/audio.flac", "score.abc": "instrumental.abc", "receipt.json": "receipt.json"}
        if name not in paths:
            raise HTTPException(404, "Unknown retained artifact")
        return FileResponse(root / job_id / paths[name])

    worker = threading.Thread(target=work, name="music-journal", daemon=False)
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
        gpu(free)
        executor.shutdown(wait=True, cancel_futures=False)
        os.close(owner_fd)

    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_MUSIC_CONFIG"]).read_text())
    os.environ["CUDA_VISIBLE_DEVICES"] = config["cudaVisibleDevice"]
    uvicorn.run(create_app(config), host="0.0.0.0", port=config["port"], timeout_graceful_shutdown=None)


if __name__ == "__main__":
    main()
