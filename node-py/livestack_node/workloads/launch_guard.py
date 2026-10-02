"""Consumer guard: authenticate a local launch, write one current class receipt."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import sys

from .launch_contract import MAX_BYTES, REGISTRY, environment_request, verify_launch
from .compilation_policy import CLASSES
from .model import WorkloadError, encode


def write_current_receipt(output, compilation_class, value):
    if not output or not Path(output).is_dir():
        raise WorkloadError('compilation_launch_receipt_directory_unavailable', 503)
    # Six finite class names (CLASSES), one atomically replaced current receipt each.
    # Existing attempt workspace/artifact retention owns these files' lifetime.
    path = Path(output)/('compilation-'+compilation_class+'.json')
    temporary = path.with_suffix('.tmp')
    raw = encode(value, MAX_BYTES)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkloadError('compilation_launch_receipt_busy', 503) from error
        with temporary.open('w') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


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
