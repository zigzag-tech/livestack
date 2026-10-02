"""Installed enrollment handler: return verifiable host and cgroup evidence."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def limits():
    """The limits the kernel enforces on this process, in the cgroup files'
    vocabulary. Windows: the attempt's Job Object, read from the kernel
    (openspec/changes/windows-host-worker)."""
    if sys.platform == 'win32':
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import windows_proc
        # By name, then proven by the kernel: the innermost job may be a venv
        # launcher's own, so "the current job" is not necessarily the attempt's.
        handle = windows_proc.Job.open(os.environ.get('HARMONY_JOB_OBJECT', ''))
        if handle is None or not handle.contains(windows_proc._k32().GetCurrentProcess()):
            raise SystemExit('probe: not inside the attempt job %r' % os.environ.get('HARMONY_JOB_OBJECT'))
        job = handle.limits()
        # cpu.max shape: "<quota> <period>" with the period 100000 us.
        return dict(memory_max=str(job['memory_bytes']),
                    cpu_max='%d 100000' % round(job['cpu_rate']/10000*(os.cpu_count() or 1)*100000),
                    tasks_max=str(job['tasks']), isolation='windows-job-object')
    group = next(line.split('::', 1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines()
                 if line.startswith('0::'))
    cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
    return dict(memory_max=(cgroup/'memory.max').read_text().strip(),
                cpu_max=(cgroup/'cpu.max').read_text().strip())


def main():
    source = Path(os.environ['HARMONY_INPUT'])
    output = Path(os.environ['HARMONY_OUTPUT'])
    report = dict(version=1, host=socket.gethostname(), attempt=os.environ['HARMONY_ATTEMPT'],
                  python=sys.version.split()[0], cpu_count=os.cpu_count(), **limits(),
                  source_manifest_digest=hashlib.sha256((source/'.harmony-source.json').read_bytes()).hexdigest())
    (output/'probe.json').write_text(json.dumps(report, sort_keys=True)+'\n')
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
