#!/usr/bin/env python3
"""Stage (and optionally activate) a handler release on a workload authority.

    tools/stage-handler-release.py --config ~/.config/livestack-workloads/authority.json \\
        --descriptor <name>-<digest>.json [--activate]

The descriptor is what a handler package builder writes (handler_id, release_digest,
archive_digest, archive_path relative to the descriptor's parent's parent, manifest). The admin
token is read from the authority config file (never an argument, never printed). Staging is
idempotent; activation sends the observed registry generation, so a concurrent change refuses
by name instead of being overwritten (see HandlerReleaseRegistry.activate).
"""
import argparse
import json
import secrets
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'node-py'))
from livestack_node.workloads.client import WorkloadClient  # noqa: E402
from livestack_node.workloads.model import WorkloadError  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--descriptor', required=True, type=Path)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    admin = next(p for p in config['principals'] if p['role'] == 'admin')
    base = f"http://{config.get('bind', '127.0.0.1')}:{config.get('port', 8802)}"
    client = WorkloadClient(base, admin['token'])
    descriptor = json.loads(args.descriptor.read_text())
    archive = args.descriptor.parent.parent/descriptor['archive_path']
    data = archive.read_bytes()
    request = urllib.request.Request(
        f"{base}/v1/workloads/objects/{descriptor['archive_digest']}", data=data, method='PUT',
        headers={'Authorization': 'Bearer '+admin['token'], 'Content-Length': str(len(data))})
    urllib.request.urlopen(request, timeout=120).read()
    try:
        staged = client.request('handler-releases/stage', dict(
            manifest=descriptor['manifest'], release_digest=descriptor['release_digest'],
            archive_digest=descriptor['archive_digest'], archive_bytes=len(data)))
        print('staged', json.dumps(staged, sort_keys=True))
        if args.activate:
            generation = client.request('handler-releases/status')['generation']
            receipt = client.request('handler-releases/activate', dict(
                request_id=secrets.token_hex(16), expected_generation=generation,
                handler_id=descriptor['handler_id'], release_digest=descriptor['release_digest']))
            print('activated', json.dumps(receipt, sort_keys=True))
    except WorkloadError as exc:
        print(f'refused: {exc} (HTTP {exc.status})', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
