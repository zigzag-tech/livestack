"""Product-adapter CLI: reuse the authenticated workload and archive contracts."""
import argparse
import json
from pathlib import Path
import sys

from .archive import capture
from .client import WorkloadClient
from .model import WorkloadError
from .transfer import InputTransfer


def read_json(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('CLI input exceeds byte bound')
    return json.loads(path.read_bytes())


def cancel_jobs(client, job_ids):
    """Cancel each job; one refusal never stops the rest, and never hides itself."""
    outcomes = []
    for job_id in job_ids:
        try:
            job = client.cancel(job_id)
            outcomes.append({'job': job_id, 'state': job['state'], 'reason': job.get('reason')})
        except (WorkloadError, ValueError) as error:
            outcomes.append({'job': job_id, 'error': str(error)})
    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    commands = parser.add_subparsers(dest='operation', required=True)
    bundle = commands.add_parser('bundle')
    bundle.add_argument('root'); bundle.add_argument('inventory'); bundle.add_argument('output')
    upload = commands.add_parser('upload'); upload.add_argument('path')
    submit = commands.add_parser('submit'); submit.add_argument('request')
    selection = submit.add_mutually_exclusive_group()
    selection.add_argument('--environment-key')
    selection.add_argument('--environment-handle')
    selection.add_argument('--no-environment', action='store_true')
    submit.add_argument('--json', action='store_true', help='emit one JSON result (the default)')
    status = commands.add_parser('get'); status.add_argument('job')
    listing = commands.add_parser('list'); listing.add_argument('--state', action='append')
    cancel = commands.add_parser('cancel'); cancel.add_argument('jobs', nargs='+')
    download = commands.add_parser('download')
    download.add_argument('digest'); download.add_argument('destination')
    environment = commands.add_parser('environment')
    environment_commands = environment.add_subparsers(dest='environment_operation', required=True)
    environment_get = environment_commands.add_parser('get'); environment_get.add_argument('handle')
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
            request = read_json(args.request, 65536)
            current = request.get('environment')
            if args.environment_key is not None:
                selected = {'key': args.environment_key, 'reuse': 'prefer'}
                if current is not None and current != selected:
                    parser.error('environment in request conflicts with CLI environment selector')
                request['version'] = 3
                request['environment'] = selected
            elif args.environment_handle is not None:
                selected = {'handle': args.environment_handle, 'reuse': 'prefer'}
                if current is not None and current != selected:
                    parser.error('environment in request conflicts with CLI environment selector')
                request['version'] = 3
                request['environment'] = selected
            elif args.no_environment:
                if current is not None:
                    parser.error('--no-environment conflicts with environment in request')
            result = client.submit(request)
        elif args.operation == 'get':
            result = client.get(args.job)
        elif args.operation == 'list':
            result = [{key: job.get(key) for key in ('id', 'state', 'reason', 'created')}
                      | {'attempts': len(job.get('attempts') or []), 'handler': (job.get('spec') or {}).get('handler'), 'key': (job.get('spec') or {}).get('key')}
                      for job in client.list_jobs() if not args.state or job.get('state') in args.state]
        elif args.operation == 'cancel':
            result = cancel_jobs(client, args.jobs)
        elif args.operation == 'environment':
            result = client.get_environment(args.handle)
        else:
            result = {'path': str(transfer.get(args.digest, args.destination))}
    print(json.dumps(result, separators=(',', ':')))
    if args.operation == 'cancel' and any('error' in item for item in result):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except WorkloadError as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)
