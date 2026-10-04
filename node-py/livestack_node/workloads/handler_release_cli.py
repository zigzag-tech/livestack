"""Operator CLI for staging and switching immutable handler packages."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from .client import WorkloadClient
from .handler_release import validate_manifest
from .model import WorkloadError
from .transfer import InputTransfer


def _config(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get('authority'), str) or not isinstance(value.get('token'), str):
        raise WorkloadError('operator config must contain authority and token')
    return value


def _stage(client, bundle, handler_id):
    root = Path(bundle).resolve()
    manifest = json.loads((root/'manifest.json').read_bytes())
    descriptor = manifest.get('handler_releases', {}).get(handler_id)
    if not isinstance(descriptor, dict):
        raise WorkloadError('bundle does not contain requested handler')
    archive = root/descriptor['archive_path']
    descriptor_file = archive.with_suffix('.json')
    package = json.loads(descriptor_file.read_bytes())
    checked = validate_manifest(package['manifest'], descriptor['release_digest'])
    if package['archive_digest'] != descriptor['archive_digest']:
        raise WorkloadError('bundle release descriptor archive digest mismatch')
    uploaded = InputTransfer(client, max_bytes=2*1024**3).put(archive)
    if uploaded['digest'] != descriptor['archive_digest']:
        raise WorkloadError('bundle archive changed after it was described')
    return client.request('handler-releases/stage', dict(manifest=checked['manifest'],
        release_digest=checked['release_digest'], archive_digest=uploaded['digest'], archive_bytes=uploaded['size']))


def _activate(client, args, *, rollback=False):
    body = dict(request_id=args.request_id or uuid.uuid4().hex,
                expected_generation=args.expected_generation, handler_id=args.handler,
                release_digest=args.digest)
    return client.request('handler-releases/rollback' if rollback else 'handler-releases/activate', body)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='operator JSON containing authority and token')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    stage = sub.add_parser('stage')
    stage.add_argument('--bundle', required=True)
    stage.add_argument('--handler', required=True)
    for name in ('activate', 'rollback'):
        command = sub.add_parser(name)
        command.add_argument('--handler', required=True)
        command.add_argument('--digest', required=True)
        command.add_argument('--expected-generation', required=True, type=int)
        command.add_argument('--request-id')
    args = parser.parse_args(argv)
    try:
        config = _config(args.config)
        client = WorkloadClient(config['authority'], config['token'], timeout=config.get('timeout', 60),
                                edge_key=config.get('edge_key'))
        try:
            if args.command == 'status':
                result = client.request('handler-releases/status')
            elif args.command == 'stage':
                result = _stage(client, args.bundle, args.handler)
            else:
                result = _activate(client, args, rollback=args.command == 'rollback')
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        finally:
            client.close()
    except (OSError, ValueError, KeyError, WorkloadError) as error:
        print(f'handler_release_command_refused: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
