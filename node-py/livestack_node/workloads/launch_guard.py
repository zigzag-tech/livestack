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
    # Five finite class names, one atomically replaced current receipt each.
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
    if compilation_class not in CLASSES:
        raise WorkloadError('compilation_launch_class_invalid', 403)
    request = None
    output = os.environ.get('HARMONY_OUTPUT')
    try:
        request = environment_request(compilation_class)
        receipt = verify_launch(request, registry_path=registry_path)
        value = dict(receipt, admitted=True, **{'class': compilation_class})
        write_current_receipt(output, compilation_class, value)
        logging.info('compilation_launch_verified: worker=%s attempt=%s class=%s',
                     receipt['worker'], receipt['attempt_id'], compilation_class)
        return receipt
    except WorkloadError as error:
        logging.error('compilation_launch_refused: class=%s reason=%s', compilation_class, str(error)[:1024])
        if output and Path(output).is_dir():
            refusal = dict(version=1, admitted=False, error=str(error)[:1024], **{'class': compilation_class})
            if request is not None:
                refusal.update({key: request[key] for key in ('worker', 'host', 'policy_revision',
                    'job_id', 'attempt_id', 'input_digest')})
            # A stale success cannot look like the latest launch. A receipt-write
            # failure itself remains a refusal; no caller gets permission.
            write_current_receipt(output, compilation_class, refusal)
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
