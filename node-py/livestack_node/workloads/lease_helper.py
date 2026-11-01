"""Fleet residency lease for workload handlers that need a model on the worker.

A GPU-bound handler admits through $HARMONY_FLEET_URL/fleet/admit under
$HARMONY_OWNER, heartbeats the lease it gets back, and releases it when the
job ends. Leases the handler admitted are recorded in
$HARMONY_OUTPUT/leases.json; the worker's cleanup releases any still recorded
there even if the handler crashed, so capacity cannot outlive the attempt.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import threading
import time
import urllib.error

from livestack_node import transport

from .model import WorkloadError, progress as validate_progress


def _post(url, token, body):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    target, path = transport.split_target(url)
    try:
        _status, _headers, raw = transport.dial(
            target, 'POST', path, headers=headers,
            body=json.dumps(body).encode(), timeout=30)
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(65536)).get('error') or error.reason
        except (ValueError, AttributeError):
            detail = str(error.reason)
        raise WorkloadError(str(detail)[:512], error.code) from error


def _record_lease(lease_id, output_dir=None):
    path = Path(output_dir or os.environ.get('HARMONY_OUTPUT', ''))/'leases.json'
    try:
        leases = json.loads(path.read_text())
    except (OSError, ValueError):
        leases = []
    leases.append(lease_id)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(leases))
    os.replace(temporary, path)


def admit(fleet_url, token, owner, kind, *, regions=(), sla='batch', estimate_s=3600,
          output_dir=None):
    """Admit a residency lease under `owner`; the fleet ledger's Grant names
    `owner`, not the worker. Refusal raises WorkloadError with the broker's
    status (429 for account quota). The lease id is recorded in
    <output_dir>/leases.json (default $HARMONY_OUTPUT) for attempt cleanup."""
    body = {'kind': kind, 'owner': owner, 'sla': sla, 'estimate': {'duration_s': estimate_s}}
    if regions:
        body['regions'] = ','.join(regions)
    result = _post(fleet_url.rstrip('/') + '/fleet/admit', token, body)
    if not result.get('granted'):
        raise WorkloadError('fleet refused residency: ' + str(result.get('reason'))[:256], 429)
    if result.get('lease_id'):
        _record_lease(result['lease_id'], output_dir)
    return result


def heartbeat(fleet_url, lease_id):
    """Proof of life; False means the slot is gone."""
    return bool(_post(fleet_url.rstrip('/') + f'/lease/{lease_id}/heartbeat', None, {}).get('ok'))


def _report(status=None, wall_s=None):
    """The release body: how the job went, when known. The broker records it
    as the lease's `caller_ok`/`job_wall_s` outcome and refuses (422) any
    other shape, so only what was actually measured is sent."""
    body = {}
    if status is not None:
        body['status'] = status
    if wall_s is not None:
        body['wall_s'] = round(max(0.0, float(wall_s)), 3)
    return body


def release(fleet_url, lease_id, *, status=None, wall_s=None):
    """Hand the slot back. `status` is "ok" or "failed" and `wall_s` the
    workload's wall time, when the caller knows them."""
    return bool(_post(fleet_url.rstrip('/') + f'/lease/{lease_id}/release', None,
                      _report(status, wall_s)).get('ok'))


def release_leftovers(fleet_url, leases_path, *, status=None, wall_s=None):
    """Release every lease recorded in leases.json (attempt cleanup after a
    normal or crashed handler). Returns the released ids; the record is
    consumed so a replayed completion does not re-release. `status`/`wall_s`
    describe the workload that held them, when known (a crash-recovery
    cleanup knows neither and sends neither)."""
    path = Path(leases_path)
    try:
        leases = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    released = []
    for lease_id in leases:
        if release(fleet_url, lease_id, status=status, wall_s=wall_s):
            released.append(lease_id)
    path.unlink(missing_ok=True)
    return released


_progress_state = {'last': 0.0, 'pending': None, 'timer': None}
_progress_lock = threading.Lock()
PROGRESS_MIN_INTERVAL = 1.0


def _write_progress(value):
    path = Path(os.environ['HARMONY_OUTPUT'])/'progress.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)
    _progress_state['last'] = time.monotonic()


def write_progress(value):
    """Validate and atomically write one document, with no rate limit of its own: for callers (the
    supervised wrapper) that own the limiter."""
    _write_progress(validate_progress(value))


def _flush_progress():
    with _progress_lock:
        value, _progress_state['pending'], _progress_state['timer'] = _progress_state['pending'], None, None
        if value is not None:
            _write_progress(value)


def report_progress(phase, *, detail=None, fraction=None, done=None, total=None, unit=None, message=None):
    """A Python handler's progress report (contract v1: phase, done/total/unit, fraction, message);
    the worker forwards progress.json to the authority on its next heartbeat. At most one write per
    second: faster calls keep only the latest, written when the second is up (and at exit). `message`
    is for humans: never put credentials, tokens or URLs with secrets in it. Needs an authority that
    knows contract v1 for done/total/unit/message; `phase`, `detail`, `fraction` work on any."""
    fields = dict(detail=detail or None, fraction=fraction, done=done, total=total, unit=unit, message=message)
    value = validate_progress({'phase': phase, **{k: v for k, v in fields.items() if v is not None}})
    with _progress_lock:
        wait = PROGRESS_MIN_INTERVAL - (time.monotonic() - _progress_state['last'])
        if wait <= 0 and _progress_state['timer'] is None:
            _write_progress(value)
            return
        _progress_state['pending'] = value
        if _progress_state['timer'] is None:
            timer = threading.Timer(max(wait, 0), _flush_progress)
            timer.daemon = True
            timer.start()
            _progress_state['timer'] = timer


atexit.register(_flush_progress)
