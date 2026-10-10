"""Owned, idempotent executor jobs; cancellation is settled by the executor.

Never kill the shared server or another GPU job. A cancellation request only
sets this job's cooperative signal. Physical settlement requires the worker to
return and its accelerator synchronization barrier to succeed.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from concurrent.futures import Executor
from dataclasses import dataclass, field


class OwnedJobCancelled(Exception):
    pass


class JobConflict(Exception):
    pass


class JobCapacity(Exception):
    pass


class CancellationSignal:
    def __init__(self):
        self.event = threading.Event()

    def check(self):
        if self.event.is_set():
            raise OwnedJobCancelled("owned executor job cancelled")


@dataclass
class _Job:
    job_id: str
    owner_id: str
    token_hash: bytes
    request_sha256: str | None = None
    kind: str | None = None
    state: str = "queued"
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    cancel_requested_at: float | None = None
    settled_at: float | None = None
    executor_active: bool = False
    physical_settled: bool = False
    signal: CancellationSignal = field(default_factory=CancellationSignal)
    future: object | None = None
    result: object | None = None
    error: str | None = None
    cleanup: object | None = None


class OwnedExecutorJobs:
    def __init__(self, executor: Executor, synchronize, max_jobs=256, result_sha256=None):
        self._executor = executor
        self._synchronize = synchronize
        self._max_jobs = max_jobs
        self._result_sha256 = result_sha256 or (lambda value: hashlib.sha256(value).hexdigest())
        self._jobs = {}
        self._lock = threading.RLock()
        self.instance_id = str(uuid.uuid4())

    @staticmethod
    def _token_hash(token):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("control token must contain at least 32 characters")
        return hashlib.sha256(token.encode()).digest()

    def _authorize(self, job_id, token, owner_id=None):
        job = self._jobs.get(job_id)
        if job is None or not hmac.compare_digest(job.token_hash, self._token_hash(token)) or (
            owner_id is not None and owner_id != job.owner_id
        ):
            raise KeyError("unknown owned executor job job")
        return job

    def _new(self, job_id, owner_id, token):
        if not owner_id.strip():
            raise ValueError("owner_id must not be blank")
        uuid.UUID(job_id)
        if len(self._jobs) >= self._max_jobs:
            raise JobCapacity("owned executor receipt capacity reached")
        job = _Job(job_id, owner_id, self._token_hash(token))
        self._jobs[job_id] = job
        return job

    def submit(self, job_id, owner_id, token, request_sha256, kind, worker, cleanup=None):
        with self._lock:
            if job_id in self._jobs:
                job = self._authorize(job_id, token, owner_id)
                if job.request_sha256 not in (None, request_sha256):
                    raise JobConflict("job ID already belongs to a different request")
                if job.request_sha256 is not None:
                    if cleanup is not None:
                        cleanup()
                    return self._receipt(job)
            else:
                job = self._new(job_id, owner_id, token)
            job.request_sha256 = request_sha256
            job.kind = kind
            job.cleanup = cleanup
            # A cancel-before-submit tombstone forbids delayed acceptance from
            # starting GPU work after the client lost the submit response.
            if job.signal.event.is_set():
                if cleanup is not None:
                    cleanup()
                return self._receipt(job)
            job.future = self._executor.submit(self._run, job, worker)
            job.future.add_done_callback(lambda future: self._finished(job, future))
            return self._receipt(job)

    def _finished(self, job, future):
        with self._lock:
            job.executor_active = False
            cleanup_failure = None
            try:
                if job.cleanup is not None:
                    job.cleanup()
            except Exception as exc:
                cleanup_failure = exc
            if future.cancelled():
                job.state = "failed" if cleanup_failure else "cancelled"
                job.error = str(cleanup_failure) if cleanup_failure else None
                job.physical_settled = True  # the executor never started it
                job.settled_at = time.time()
                return
            try:
                result, failure, synchronized = future.result()
            except BaseException:
                result, failure, synchronized = None, RuntimeError("executor settlement unconfirmed"), False
            failure = failure or cleanup_failure
            job.physical_settled = synchronized
            job.settled_at = time.time() if synchronized else None
            if not synchronized:
                job.state, job.error = "failed", "accelerator settlement failed"
            elif job.signal.event.is_set():
                job.state = "cancelled"
            elif failure is not None:
                job.state, job.error = "failed", str(failure)
            else:
                job.state, job.result = "completed", result

    def _run(self, job, worker):
        with self._lock:
            job.executor_active = True
            job.started_at = time.time()
            job.state = "cancel_requested" if job.signal.event.is_set() else "running"
        result, failure, synchronized = None, None, False
        try:
            job.signal.check()
            result = worker(job.signal)
        except Exception as exc:
            failure = exc
        finally:
            try:
                self._synchronize()
                synchronized = True
            except Exception as exc:
                failure = exc
        return result, failure, synchronized

    def cancel(self, job_id, token, owner_id):
        with self._lock:
            if job_id not in self._jobs:
                job = self._new(job_id, owner_id, token)
                job.signal.event.set()
                job.cancel_requested_at = time.time()
                job.state, job.physical_settled = "cancelled", True
                job.settled_at = time.time()
                return self._receipt(job)
            job = self._authorize(job_id, token, owner_id)
            if job.state in ("completed", "cancelled", "failed"):
                return self._receipt(job)
            job.signal.event.set()
            job.cancel_requested_at = job.cancel_requested_at or time.time()
            job.state = "cancel_requested"
            if job.future is not None:
                job.future.cancel()  # only succeeds while THIS job is queued
            return self._receipt(job)

    def status(self, job_id, token):
        with self._lock:
            return self._receipt(self._authorize(job_id, token))

    def result(self, job_id, token):
        with self._lock:
            job = self._authorize(job_id, token)
            if job.state != "completed" or not job.physical_settled:
                raise JobConflict("owned executor job has no physically settled result")
            return job.result

    def _receipt(self, job):
        return {
            "job_protocol_version": 1, "server_instance_id": self.instance_id,
            "job_id": job.job_id, "owner_id": job.owner_id, "kind": job.kind,
            "request_sha256": job.request_sha256, "state": job.state,
            "submitted_at": job.submitted_at, "started_at": job.started_at,
            "cancel_requested_at": job.cancel_requested_at, "settled_at": job.settled_at,
            "executor_active": job.executor_active, "physical_settled": job.physical_settled,
            "result_sha256": self._result_sha256(job.result) if job.result is not None else None,
            "error": job.error,
        }
