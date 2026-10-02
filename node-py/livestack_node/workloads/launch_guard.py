"""Consumer guard: authenticate a local launch, write one current class receipt."""
import argparse
import json
import logging
import os
import stat
from pathlib import Path
import sys

from .launch_contract import MAX_BYTES, REGISTRY, environment_request, verify_launch
from .compilation_policy import CLASSES
from .model import WorkloadError, encode
if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl

# Absent on Windows, where symlinks need a privilege the worker account lacks
# and handles are not inherited by default.
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)


def _lock(stream):
    if sys.platform == 'win32':
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError(str(error)) from error
    else:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def write_current_receipt(output, compilation_class, value):
    if not isinstance(compilation_class,str) or compilation_class not in CLASSES:
        raise WorkloadError('compilation_launch_class_invalid',403)
    if not output or not Path(output).is_dir():
        raise WorkloadError('compilation_launch_receipt_directory_unavailable', 503)
    # Six finite class names (CLASSES), one atomically replaced current receipt each.
    # Existing attempt workspace/artifact retention owns these files' lifetime.
    path = Path(output)/('compilation-'+compilation_class+'.json')
    temporary = path.with_suffix('.tmp')
    raw = encode(value, MAX_BYTES)
    lock_path=path.with_suffix('.lock')
    try:
        descriptor=os.open(lock_path,os.O_WRONLY|os.O_CREAT|_NOFOLLOW|_NONBLOCK|_CLOEXEC,0o600)
    except OSError as error:
        raise WorkloadError('compilation_launch_receipt_lock_invalid',503) from error
    with os.fdopen(descriptor,'w') as lock:
        metadata=os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size!=0 or metadata.st_nlink!=1:
            raise WorkloadError('compilation_launch_receipt_lock_invalid',503)
        try:
            _lock(lock)
        except BlockingIOError as error:
            raise WorkloadError('compilation_launch_receipt_busy', 503) from error
        try:
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|_NOFOLLOW|_CLOEXEC,0o600)
        except OSError as error:
            raise WorkloadError('compilation_launch_receipt_staging_invalid',503) from error
        try:
            with os.fdopen(descriptor,'w') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:
            # Remove only this invocation's exclusively created staging file.
            if temporary.exists():temporary.unlink()


def require_compilation(compilation_class, *, registry_path=REGISTRY):
    return require_compilations([compilation_class], registry_path=registry_path)


def require_compilations(compilation_classes, *, registry_path=REGISTRY):
    """Authorize a finite class set from one authenticated live receipt."""
    if (not isinstance(compilation_classes, (list, tuple)) or
            not 1 <= len(compilation_classes) <= len(CLASSES) or
            any(not isinstance(item, str) or item not in CLASSES for item in compilation_classes) or
            len(set(compilation_classes)) != len(compilation_classes)):
        raise WorkloadError('compilation_launch_class_invalid', 403)
    compilation_class = compilation_classes[0]
    request = None
    output = os.environ.get('HARMONY_OUTPUT')
    try:
        request = environment_request(compilation_class)
        receipt = verify_launch(request, registry_path=registry_path)
        if any(item not in receipt.get('classes', []) for item in compilation_classes):
            raise WorkloadError('compilation_class_not_reserved', 403)
        for item in compilation_classes:
            value = dict(receipt, admitted=True, **{'class': item})
            write_current_receipt(output, item, value)
        logging.info('compilation_launch_verified: worker=%s attempt=%s classes=%s',
                     receipt['worker'], receipt['attempt_id'], ','.join(compilation_classes))
        return receipt
    except WorkloadError as error:
        logging.error('compilation_launch_refused: classes=%s reason=%s',
                      ','.join(compilation_classes), str(error)[:1024])
        if output and Path(output).is_dir():
            refusal = dict(version=1, admitted=False, error=str(error)[:1024], **{'class': compilation_class})
            if request is not None:
                refusal.update({key: request[key] for key in ('worker', 'host', 'policy_revision',
                    'job_id', 'attempt_id', 'input_digest')})
            # A stale success cannot look like the latest launch. A receipt-write
            # failure itself remains a refusal; no caller gets permission.
            for item in compilation_classes:
                write_current_receipt(output, item, dict(refusal, **{'class': item}))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--class', dest='compilation_class', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        receipt = require_compilation(args.compilation_class)
        print(json.dumps(receipt, sort_keys=True))
    except (WorkloadError, OSError, ValueError) as error:
        logging.error('compilation_launch_refused: %s', str(error)[:1024])
        sys.exit(75)


if __name__ == '__main__':
    main()
