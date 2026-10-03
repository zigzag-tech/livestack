"""Stream immutable inputs over the authenticated workload connection."""
import logging
from .object_download import send_object

from .model import WorkloadError
from .blob_references import route_reference

OBJECT_UPLOAD_IDLE_TIMEOUT_SECONDS = 20 * 60


def attempt_owner(store, principal, headers, digest=None):
    """Workers can access only content of their CURRENT execution attempt."""
    try:
        fence = int(headers.get('X-Workload-Fence', ''))
    except ValueError:
        raise WorkloadError('attempt authorization required', 403)
    with store.transaction() as db:
        store._expire(db, store.clock())
        store._worker(db, principal.worker, headers.get('X-Workload-Boot'))
        a = db.execute("SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? AND fence=? AND state='running'",
                       (headers.get('X-Workload-Attempt'), principal.worker,
                        headers.get('X-Workload-Boot'), fence)).fetchone()
        if not a:
            raise WorkloadError('attempt authorization expired', 409)
        job = store._job(db, a['job'])
        inputs = {job['spec']['input_digest'], *(item['digest'] for item in job['spec'].get('input_objects', []))}
        if digest is not None and digest not in inputs:
            raise WorkloadError('content is not an input of this attempt', 403)
        return job['owner']


def route_object(handler, principal, method, parts):
    if route_reference(handler, principal, method, parts):
        return True
    if len(parts) != 2 or parts[0] != 'objects':
        return False
    blobs, store = handler.server.blobs, handler.server.store
    digest = blobs.digest(parts[1])
    if method == 'GET':
        owner = attempt_owner(store, principal, handler.headers, digest) if principal.role == 'worker' else principal.id
        with blobs.open(owner, digest) as (stream, size):
            send_object(handler, stream, size, digest)
    elif method == 'PUT':
        owner = attempt_owner(store, principal, handler.headers) if principal.role == 'worker' else principal.id
        if handler.headers.get('Transfer-Encoding'):
            raise WorkloadError('transfer encoding is not supported')
        try:
            size = int(handler.headers.get('Content-Length', '-1'))
        except ValueError:
            raise WorkloadError('invalid content length')
        # Handler.setup() uses a short timeout to evict idle keep-alive
        # connections. That timeout also applies while this request body is
        # being streamed; large, valid source archives can take longer than 15
        # seconds between reads on a loaded host. Match the bounded publisher
        # transfer window for the body only, then restore the keep-alive bound.
        previous_timeout = handler.connection.gettimeout()
        handler.connection.settimeout(OBJECT_UPLOAD_IDLE_TIMEOUT_SECONDS)
        try:
            result = blobs.put(owner, digest, size, handler.rfile)
        finally:
            handler.connection.settimeout(previous_timeout)
        # The verified authority CAS is canonical. A regional mirror is only a
        # best-effort cache, so it must not extend the caller's upload or keep a
        # worker attempt alive after the canonical bytes are durable.
        handler.respond(200, result)
        if handler.server.artifact_mirror:
            try:
                handler.server.artifact_mirror.put(digest, blobs.root/digest, blobs.max_object_bytes)
            except WorkloadError as error:
                # The verified CAS write is authoritative. A cache outage must
                # not make a caller repeat an already accepted object upload.
                logging.warning('artifact mirror unavailable for %s: %s', digest, error)
    else:
        raise WorkloadError('unsupported object operation', 405)
    return True
