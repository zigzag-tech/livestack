"""Product-adapter CLI: reuse the authenticated workload and archive contracts."""
import argparse
import json
from pathlib import Path

from .archive import capture
from .client import WorkloadClient
from .transfer import InputTransfer


def read_json(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('CLI input exceeds byte bound')
    return json.loads(path.read_bytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    commands = parser.add_subparsers(dest='operation', required=True)
    bundle = commands.add_parser('bundle')
    bundle.add_argument('root'); bundle.add_argument('inventory'); bundle.add_argument('output')
    upload = commands.add_parser('upload'); upload.add_argument('path')
    submit = commands.add_parser('submit'); submit.add_argument('request')
    status = commands.add_parser('get'); status.add_argument('job')
    download = commands.add_parser('download')
    download.add_argument('digest'); download.add_argument('destination')
    args = parser.parse_args()
    if args.operation == 'bundle':
        inventory = read_json(args.inventory, 16*1024**2)
        result = capture(args.root, inventory['paths'], args.output,
                         provenance=inventory.get('provenance'))
        result = {key: result[key] for key in ('digest', 'size')}
    else:
        if not args.config:
            parser.error('--config is required for authority operations')
        config = read_json(args.config, 65536)
        client = WorkloadClient(config['authority'], config['token'], timeout=60)
        transfer = InputTransfer(client)
        if args.operation == 'upload':
            result = transfer.put(args.path)
        elif args.operation == 'submit':
            result = client.submit(read_json(args.request, 65536))
        elif args.operation == 'get':
            result = client.get(args.job)
        else:
            result = {'path': str(transfer.get(args.digest, args.destination))}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
