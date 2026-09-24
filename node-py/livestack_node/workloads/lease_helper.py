"""Fleet residency lease for workload handlers that need a model on the worker.

A GPU-bound handler admits through $HARMONY_FLEET_URL/fleet/admit under
$HARMONY_OWNER, heartbeats the lease it gets back, and releases it when the
job ends. Leases the handler admitted are recorded in
$HARMONY_OUTPUT/leases.json; the worker's cleanup releases any still recorded
there even if the handler crashed, so capacity cannot outlive the attempt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .model import WorkloadError, progress as validate_progress


def _post(url, token, body):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
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


def report_progress(phase, *, detail=None, fraction=None):
    """A Python handler's progress report; the worker forwards progress.json
    to the authority on its next heartbeat."""
    value = validate_progress({'phase': phase, **({'detail': detail} if detail else {}),
                               **({'fraction': fraction} if fraction is not None else {})})
    path = Path(os.environ['HARMONY_OUTPUT'])/'progress.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)
