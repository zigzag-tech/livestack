"""Durable IndexTTS2.5 speech jobs on one actually admitted Harmony device.

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


from .protocol import validate_request


def create_app(config):
    root = Path(config["stateRoot"])
    root.mkdir(parents=True, exist_ok=True)
    owner_fd = os.open(root / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = Path(config["tokenFile"]).read_text().strip()
    if len(token) < 32:
        raise ValueError("Private installed node credential is absent")
    source_root = Path(config["sourceRoot"])
    sys.path.insert(0, str(source_root))
    from indextts.infer_v2_5 import IndexTTS2
    import numpy as np
    import soundfile as sf
    import torch
    # The release/model inventory is independently SHA-verified during staging;
    # retain its exact identity in every accepted job rather than a model alias.
    if hashlib.sha256(Path(config["modelManifestPath"]).read_bytes()).hexdigest() != config["modelManifestSha256"]:
        raise ValueError("Pinned model inventory changed")
    from concurrent.futures import ThreadPoolExecutor

    actual_uuid = str(torch.cuda.get_device_properties(0).uuid)
    if actual_uuid.removeprefix("GPU-") != config["cudaVisibleDevice"].removeprefix("GPU-"):
        raise ValueError("Actual CUDA UUID differs from the installed physical device")

    # All CUDA work, including warm/evict, uses the same actual GPU thread.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-gpu")
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
        torch.cuda.set_per_process_memory_fraction(config["residencyBudgetBytes"] / torch.cuda.get_device_properties(0).total_memory, 0)
        pipe = IndexTTS2(cfg_path=str(Path(config["modelPath"]) / "config.yaml"),
            model_dir=config["modelPath"], device="cuda:0", use_bf16=True,
            use_cuda_kernel=False, use_deepspeed=False, use_accel=False,
            use_torch_compile=False, use_qwen_emo=False)
        torch.cuda.synchronize()
        holder["pipe"] = pipe
        return pipe

    def free():
        pipe = holder.pop("pipe", None)
        if pipe is not None:
            torch.cuda.synchronize()
            del pipe
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    attrs = {"class": "tts", "task": "text_to_speech",
        "model": "IndexTeam/IndexTTS-2.5", "revision": config["modelRevision"],
        "sample_rate": 22050, "channels": 1, "license": "bilibili-model-use-license",
        "max_concurrent": 1, "languages": ["en", "zh", "ja", "es", "ar"],
        "emotion_vector": True, "emotion_text": False,
        "duration_factor": {"min": .5, "max": 2, "exact_seconds": False},
        "interruptible_inference": False}
    unit = ManagedUnit(config["unit"], load, free, footprint=config["residencyBudgetBytes"],
        residency_policy=ResidencyPolicy.UNPINNED, min_resident=0, attributes=attrs,
        spread_group="tts", priority=30, reload_cost=60, min_residency_s=60)
    app = FastAPI(title="Harmony IndexTTS2.5 speech jobs")
    app.state.owner_fd = owner_fd
    manager, coordinator = attach(app, host_id=config["hostId"], kind="tts", units={config["unit"]: unit},
        idle_seconds=120, coload=True, gpu_call=gpu, port=config["port"], in_flight=busy,
        device_id=config["deviceId"], inventory=attrs)

    def authenticated(authorization):
        if not secrets.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "Installed tts-node credential required")

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
        import random
        import warnings
        c = body["controls"]
        reference = config["voices"][body["voiceId"]]
        if hashlib.sha256(Path(reference["path"]).read_bytes()).hexdigest() != body["referenceSha256"]:
            raise ValueError("Registered reference bytes changed")
        random.seed(c["seed"])
        np.random.seed(c["seed"])
        torch.manual_seed(c["seed"])
        torch.cuda.manual_seed_all(c["seed"])
        torch.cuda.reset_peak_memory_stats()
        if cancel.is_set():
            raise InterruptedError("Cancelled before native synthesis")
        # Upstream has no owned stopping callback through every decoder stage.
        # Cancellation joins this real call and CUDA synchronization; no output
        # or lease release occurs merely because a socket/future was cancelled.
        with warnings.catch_warnings(record=True) as observed:
            result = pipe.infer(spk_audio_prompt=reference["path"], text=body["text"],
                output_path=None, lang=body["language"],
                duration_factor=c["durationFactor"], emo_vector=c.get("emotionVector"),
                emo_alpha=c["emotionAlpha"], interval_silence=c["intervalSilenceMs"],
                max_text_tokens_per_segment=c["maxTextTokensPerSegment"],
                use_random=False, use_emo_text=False, stream_return=False,
                text_normalization=True)
        torch.cuda.synchronize()
        if cancel.is_set():
            raise InterruptedError("Cancellation joined native synthesis and CUDA settlement")
        if result is None or not isinstance(result, tuple) or len(result) != 2:
            raise ValueError("Native audio result absent")
        sr, audio = result
        audio = np.asarray(audio)
        if audio.ndim == 2 and audio.shape[1] == 1:
            audio = audio[:, 0]
        if sr != 22050 or audio.ndim != 1 or audio.dtype != np.int16 or not len(audio):
            raise ValueError("Native clock/mono PCM16 contract refused")
        messages = [str(w.message) for w in observed]
        if any("max_mel_tokens" in m or "generation stopped" in m for m in messages):
            raise ValueError("Truncated native synthesis refused")
        target = path / "audio.wav"
        sf.write(target, audio, sr, subtype="PCM_16")
        payload = target.read_bytes()
        return {"audio": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
            "sampleRate": sr, "channels": 1, "frames": len(audio), "durationSeconds": len(audio) / sr},
            "model": attrs, "controls": c, "referenceSha256": body["referenceSha256"],
            "textSha256": hashlib.sha256(body["text"].encode()).hexdigest(),
            "warnings": messages, "torchPeakAllocatedBytes": torch.cuda.max_memory_allocated(),
            "torchPeakReservedBytes": torch.cuda.max_memory_reserved()}

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
                        raise InterruptedError("Cancelled after admission, before speech generation")
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
        return {"ok": True, "release": config["release"], "nodeKind": "tts", "loaded": unit.loaded,
            "incarnation": incarnation, "installation": {"digest": config["installationDigest"]}}

    @app.get("/v1/tts/capabilities")
    def capabilities():
        return {"protocolVersion": 1, **attrs, "voices": [{"id": k, "sha256": v["sha256"],
            "origin": v["origin"]} for k, v in config["voices"].items()], "source": config["sourceIdentity"]}

    @app.post("/v1/tts/jobs", status_code=202)
    def submit(body: dict = Body(...), authorization: str | None = Header(None)):
        authenticated(authorization)
        try:
            body = validate_request(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if not any(body["ownerId"].startswith(prefix) for prefix in config["allowedOwnerPrefixes"]):
            raise HTTPException(403, "Installed owner scope refused")
        reference = config["voices"].get(body["voiceId"])
        if not reference or reference["sha256"] != body["referenceSha256"]:
            raise HTTPException(403, "Installed synthetic reference identity refused")
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
        paths = {"audio.wav": "audio.wav", "receipt.json": "receipt.json"}
        if name not in paths:
            raise HTTPException(404, "Unknown retained artifact")
        return FileResponse(root / job_id / paths[name])

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
        gpu(free)
        executor.shutdown(wait=True, cancel_futures=False)
        os.close(owner_fd)

    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_TTS_CONFIG"]).read_text())
    os.environ["CUDA_VISIBLE_DEVICES"] = config["cudaVisibleDevice"]
    uvicorn.run(create_app(config), host="0.0.0.0", port=config["port"], timeout_graceful_shutdown=None)


if __name__ == "__main__":
    main()
