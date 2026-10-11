"""Explicit legacy broker abort scope; termination never means job success."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sqlite3
import os
from time import monotonic

from .provider_owner_probe import process_start_ticks

UNITS = {'livestack-hostd.service': 'broker', 'polytts.service': 'tts',
         'harmony-klein-1.service': 'image'}
PROPERTIES = ('Id', 'InvocationID', 'MainPID', 'ControlGroup', 'ActiveState',
              'KillMode', 'FragmentPath', 'DropInPaths', 'NeedDaemonReload', 'Job')


def validate_abort_plan(value, registered_inventory):
    if not isinstance(value, dict) or set(value) != {'version', 'mode', 'units', 'retainedFiles'} or value['version'] != 1 or value['mode'] != 'abort':
        raise ValueError('broker_closed_abort_plan_required')
    units = value['units']
    if not isinstance(units, list) or not 1 <= len(units) <= 3 or len({row.get('unit') for row in units if isinstance(row, dict)}) != len(units):
        raise ValueError('broker_bounded_unique_abort_units_required')
    for row in units:
        if not isinstance(row, dict) or set(row) != {'unit', 'role', 'unitSha256', 'bootId', 'invocationId', 'mainPid', 'startTicks', 'controlGroup', 'acceptedWork', 'impact'}:
            raise ValueError('broker_closed_predecessor_identity_required')
        if UNITS.get(row['unit']) != row['role'] or row['controlGroup'] != '/system.slice/' + row['unit']:
            raise ValueError('broker_exact_owned_abort_unit_required')
        if not re.fullmatch('[a-f0-9]{64}', row['unitSha256']) or not re.fullmatch('[a-f0-9]{32}', row['invocationId']) or not re.fullmatch('[a-f0-9-]{36}', row['bootId']):
            raise ValueError('broker_exact_predecessor_hashes_required')
        if type(row['mainPid']) is not int or row['mainPid'] <= 0 or type(row['startTicks']) is not int or row['startTicks'] <= 0:
            raise ValueError('broker_exact_live_predecessor_required')
        if row['acceptedWork'] != 'unknown' or row['impact'] != 'operator-aborted-unknown-result':
            raise ValueError('broker_legacy_unknown_work_must_remain_explicit')
    if not any(row['role'] == 'broker' for row in units):
        raise ValueError('broker_predecessor_required')
    if not isinstance(registered_inventory, list) or not registered_inventory:
        raise ValueError('broker_registered_retention_inventory_required')
    files = value['retainedFiles']
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        raise ValueError('broker_retained_source_history_required')
    for row in files:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256', 'size', 'role', 'unit'} or row['role'] not in ('source', 'unit', 'configuration', 'operations', 'wal', 'ledger', 'lease-snapshot'):
            raise ValueError('broker_closed_retention_record_required')
        path = Path(row['path'])
        if not path.is_absolute() or path.resolve() != path or not re.fullmatch('[a-f0-9]{64}', row['sha256']) or type(row['size']) is not int or row['size'] < 0:
            raise ValueError('broker_exact_retained_bytes_required')
    if len({row['path'] for row in files}) != len(files):
        raise ValueError('broker_duplicate_retention_path')
    canonical = lambda rows: sorted(json.dumps(row, sort_keys=True, separators=(',', ':')) for row in rows)
    if canonical(files) != canonical(registered_inventory):
        raise ValueError('broker_registered_retention_inventory_changed')
    for unit in units:
        required = {'source', 'unit', 'configuration'}
        if unit['role'] == 'broker':
            required |= {'operations', 'ledger', 'lease-snapshot'}
        retained = {row['role'] for row in files if row['unit'] == unit['unit']}
        if not required <= retained:
            raise ValueError('broker_incomplete_retention_coverage')
    if any(row['unit'] not in {unit['unit'] for unit in units} for row in files):
        raise ValueError('broker_foreign_retention_unit')
    return value


def verify_registered_dropins(row, fields, retained_files=()):
    """Only exact unit files already bound by the registered retention plan."""
    prefix = '/etc/systemd/system/' + row['unit'] + '.d/'
    expected = [item for item in retained_files if item['unit'] == row['unit']
                and item['role'] == 'unit' and item['path'].startswith(prefix)]
    if any('/' in item['path'][len(prefix):] or not item['path'].endswith('.conf') for item in expected):
        raise ValueError('broker_registered_dropin_path_invalid')
    paths = fields['DropInPaths'].split()
    if len(paths) != len(set(paths)) or set(paths) != {item['path'] for item in expected}:
        raise ValueError('broker_registered_dropins_changed')
    for item in expected:
        path = Path(item['path'])
        for ancestor in (path.parent, *path.parents):
            info = ancestor.lstat()
            if ancestor.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('broker_registered_dropin_ancestry_changed')
        stat = path.lstat()
        if path.is_symlink() or path.resolve() != path or stat.st_uid != 0 or stat.st_mode & 0o022 or stat.st_size != item['size'] or sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('broker_registered_dropin_bytes_changed')


def inspect_predecessor(row, retained_files=()):
    """Read-only authoritative manager plus kernel identity, never health-only."""
    output = subprocess.check_output(['/usr/bin/systemctl', 'show', row['unit'],
        *['--property=' + field for field in PROPERTIES]], timeout=20, text=True)
    if len(output.encode()) > 16384:
        raise ValueError('broker_unit_observation_bound')
    fields = dict(line.split('=', 1) for line in output.splitlines())
    if set(fields) != set(PROPERTIES) or fields['Id'] != row['unit'] or fields['ControlGroup'] != row['controlGroup'] or fields['KillMode'] != 'control-group' or fields['NeedDaemonReload'] != 'no':
        raise ValueError('broker_owned_unit_manager_changed')
    verify_registered_dropins(row, fields, retained_files)
    if fields['InvocationID'] != row['invocationId'] or fields['MainPID'] != str(row['mainPid']) or fields['ActiveState'] != 'active' or fields['Job'] not in ('', '0'):
        raise ValueError('broker_predecessor_incarnation_changed')
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != row['bootId'] or process_start_ticks(row['mainPid']) != row['startTicks']:
        raise ValueError('broker_predecessor_kernel_identity_changed')
    if sha256(Path(fields['FragmentPath']).read_bytes()).hexdigest() != row['unitSha256']:
        raise ValueError('broker_predecessor_unit_bytes_changed')
    group = Path('/sys/fs/cgroup') / row['controlGroup'].lstrip('/')
    processes = []
    for file in [group / 'cgroup.procs', *sorted(group.rglob('cgroup.procs'))]:
        for pid in map(int, file.read_text().split()):
            processes.append({'pid': pid, 'startTicks': process_start_ticks(pid)})
    return {'manager': fields, 'processes': sorted(processes, key=lambda item: item['pid'])}


def verify_terminated(row, prior, retained_files=()):
    """Actual stopped unit and recursive leaf/PID proof, not job completion.

    Caller reads `prior` from the source-bound private submitted journal. No
    supplied drained flag, timeout or missing owner socket is credited.
    """
    output = subprocess.check_output(['/usr/bin/systemctl', 'show', row['unit'],
        *['--property=' + field for field in PROPERTIES]], timeout=20, text=True)
    if len(output.encode()) > 16384:
        raise ValueError('broker_unit_observation_bound')
    fields = dict(line.split('=', 1) for line in output.splitlines())
    if set(fields) != set(PROPERTIES) or fields['Id'] != row['unit'] or fields['InvocationID'] != row['invocationId'] or fields['MainPID'] != '0' or fields['ActiveState'] not in ('inactive', 'failed') or fields['Job'] not in ('', '0'):
        raise ValueError('broker_abort_not_physically_terminal')
    if fields['KillMode'] != 'control-group' or fields['NeedDaemonReload'] != 'no' or sha256(Path(fields['FragmentPath']).read_bytes()).hexdigest() != row['unitSha256']:
        raise ValueError('broker_aborted_unit_bytes_changed')
    verify_registered_dropins(row, fields, retained_files)
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != row['bootId'] or prior['manager']['InvocationID'] != row['invocationId'] or prior['manager']['MainPID'] != str(row['mainPid']):
        raise ValueError('broker_abort_kernel_incarnation_changed')
    if not prior['processes'] or not any(item['pid'] == row['mainPid'] and item['startTicks'] == row['startTicks'] for item in prior['processes']):
        raise ValueError('broker_original_live_witness_required')
    for process in prior['processes']:
        try:
            if process_start_ticks(process['pid']) == process['startTicks']:
                raise ValueError('broker_owned_process_not_reaped')
        except FileNotFoundError:
            pass
    group = Path('/sys/fs/cgroup') / row['controlGroup'].lstrip('/')
    if group.exists():
        if group.resolve() != group:
            raise ValueError('broker_owned_cgroup_alias_refused')
        for path in [group, *sorted(item for item in group.rglob('*') if item.is_dir())]:
            if (path / 'cgroup.procs').read_text().strip():
                raise ValueError('broker_owned_cgroup_not_empty')
            events = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
            if events.get('populated') != '0':
                raise ValueError('broker_owned_cgroup_not_physically_settled')
    gpu_checked = row['role'] != 'broker'
    if gpu_checked:
        gpu = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader,nounits'], timeout=20, text=True)
        if len(gpu.encode()) > 65536:
            raise ValueError('broker_gpu_observation_bound')
        active = {int(line.strip()) for line in gpu.splitlines() if line.strip()}
        if active & {item['pid'] for item in prior['processes']}:
            raise ValueError('broker_owned_gpu_process_not_settled')
    return {'unit': row['unit'], 'invocationId': row['invocationId'],
            'terminationQualified': True, 'processesReaped': True,
            'cgroupEmpty': True, 'gpuPidAbsenceChecked': gpu_checked,
            'acceptedWorkOutcome': 'operator-aborted-unknown-result',
            'jobsCompleted': False, 'remoteWorkReconciled': False}


def verify_retained_files(plan, registered_inventory):
    for row in validate_abort_plan(plan, registered_inventory)['retainedFiles']:
        path = Path(row['path'])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != row['size'] or sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('broker_retained_bytes_changed')
    return sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot_database(source, destination):
    """SQLite backup retains one committed state including live WAL content.

    This must be repeated after physical retirement before activation. A live
    pre-abort snapshot is not the final accepted-work/history preservation gate.
    Original database/WAL paths remain in place; no checkpoint or truncation.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_absolute() or source.resolve() != source or source.is_symlink() or not source.is_file():
        raise ValueError('broker_exact_original_database_required')
    if not destination.is_absolute() or destination.resolve() != destination or destination.exists():
        raise ValueError('broker_append_only_snapshot_required')
    parent = destination.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise PermissionError('broker_private_snapshot_parent_required')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    deadline = monotonic() + 20
    def progress(_status, _remaining, _total):
        if monotonic() > deadline:
            raise TimeoutError('broker_database_snapshot_control_deadline')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as original:
        with closing(sqlite3.connect(destination, timeout=10)) as retained:
            original.backup(retained, pages=64, progress=progress, sleep=0.01)
            if retained.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('broker_retained_database_integrity_refused')
    with destination.open('rb') as retained:
        os.fsync(retained.fileno())
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'path': str(destination), 'sha256': sha256(destination.read_bytes()).hexdigest(),
            'size': destination.stat().st_size, 'method': 'sqlite-consistent-backup',
            'originalPreserved': True, 'finalAfterTermination': False}
