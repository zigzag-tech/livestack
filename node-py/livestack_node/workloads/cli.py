"""Product-adapter CLI: reuse the authenticated workload and archive contracts."""
import argparse
import getpass
import json
import os
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
    commands.add_parser('workers', help='read-only fleet roster as the authority sees it')
    cancel = commands.add_parser('cancel'); cancel.add_argument('jobs', nargs='+')
    download = commands.add_parser('download')
    download.add_argument('digest'); download.add_argument('destination')
    environment = commands.add_parser('environment')
    environment_commands = environment.add_subparsers(dest='environment_operation', required=True)
    environment_get = environment_commands.add_parser('get'); environment_get.add_argument('handle')
    drain = commands.add_parser('drain', help='stop a worker taking new work, with an owner and an end')
    drain.add_argument('worker'); drain.add_argument('--owner'); drain.add_argument('--reason', default='')
    when = drain.add_mutually_exclusive_group(required=True)
    when.add_argument('--until', help='ISO-8601 time, e.g. 2026-10-09T06:00Z')
    when.add_argument('--ttl', type=float, help='seconds from now')
    drain.add_argument('--if-generation', type=int); drain.add_argument('--force', action='store_true')
    enable = commands.add_parser('enable', help='let a worker take work again')
    enable.add_argument('worker'); enable.add_argument('--owner'); enable.add_argument('--reason', default='')
    enable.add_argument('--if-generation', type=int); enable.add_argument('--force', action='store_true')
    claims = commands.add_parser('claims', help='who holds which worker, until when')
    claims.add_argument('--export-authority-json', action='store_true',
                        help='print {worker: claim_enabled} in the authority.json principal field form')
    commands.add_parser('reload-status', help='is the last authority.json edit applied?')
    rollout = commands.add_parser('rollout')
    rollout_commands = rollout.add_subparsers(dest='rollout_operation', required=True)
    rollout_commands.add_parser('status')
    rollout_spec = rollout_commands.add_parser('spec'); rollout_spec.add_argument('file')
    rollout_spec.add_argument('--if-generation', type=int)
    rollout_unit = rollout_commands.add_parser('unit'); rollout_unit.add_argument('file')
    unit = commands.add_parser('unit')
    unit_commands = unit.add_subparsers(dest='unit_operation', required=True)
    unit_build = unit_commands.add_parser('build', help='assemble a deployment unit from what is on disk')
    unit_build.add_argument('--release-dir', required=True); unit_build.add_argument('--handlers-root', required=True)
    unit_build.add_argument('--digest', action='append', default=[]); unit_build.add_argument('--verifier-dir')
    unit_build.add_argument('--capture-size', type=int, required=True)
    unit_build.add_argument('--capture-cap', type=int, required=True)
    unit_build.add_argument('--min-authority', required=True)
    unit_build.add_argument('--built-from', action='append', default=[])
    args = parser.parse_args()
    if args.operation == 'unit':
        from . import unit as deployment_unit
        result = deployment_unit.build(
            release_dir=args.release_dir, handlers_root=args.handlers_root, digests=args.digest,
            verifier_dir=args.verifier_dir, capture_size=args.capture_size, capture_cap=args.capture_cap,
            min_authority=args.min_authority, built_from=args.built_from or ['0000000'])
    elif args.operation == 'bundle':
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
        elif args.operation == 'workers':
            result = client.roster()
        elif args.operation in ('drain', 'enable'):
            body = {k: v for k, v in dict(
                owner=args.owner or os.environ.get('LIVESTACK_CLAIM_OWNER') or f'cli:{getpass.getuser()}',
                reason=args.reason, if_generation=args.if_generation, force=args.force or None).items()
                if v is not None}
            if args.operation == 'drain':
                if args.ttl is not None:
                    body['ttl_seconds'] = args.ttl
                else:
                    body['until'] = args.until
            result = client.request(f'claims/{args.worker}/{args.operation}', body)
        elif args.operation == 'claims':
            result = client.request('claims')
            if args.export_authority_json:
                result = {c['worker']: not c['draining'] for c in result['claims']}
        elif args.operation == 'reload-status':
            result = client.request('reload/status')
        elif args.operation == 'rollout':
            if args.rollout_operation == 'status':
                result = client.request('rollout')
            elif args.rollout_operation == 'spec':
                result = client.request('rollout/spec', dict(
                    spec=read_json(args.file, 65536), if_generation=args.if_generation))
            else:
                result = client.request('rollout/units', dict(manifest=read_json(args.file, 65536)))
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
