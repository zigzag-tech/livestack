"""Validate immutable output references inside the completion transaction."""
from .blobs import BlobStore
from .model import WorkloadError, name


def validate_artifacts(db, owner, result):
    if not isinstance(result, dict):
        raise WorkloadError('result must be an object')
    artifacts = result.get('artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) > 128:
        raise WorkloadError('result artifacts must be a list of at most 128 objects')
    names = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {'name', 'digest', 'size'}:
            raise WorkloadError('invalid artifact reference')
        key = name(artifact['name'], 'artifact name')
        digest = BlobStore.digest(artifact['digest'])
        size = artifact['size']
        if key in names or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WorkloadError('duplicate artifact name or invalid size')
        names.add(key)
        row = db.execute("SELECT b.size FROM blobs b JOIN blob_owners o USING(digest) "
                         "WHERE b.digest=? AND o.owner=? AND b.state='ready'", (digest,owner)).fetchone()
        if not row or row['size'] != size:
            raise WorkloadError('artifact is absent, foreign, or has a different size', 409)
