"""Installed enrollment handler: return verifiable host and cgroup evidence."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def main():
    source = Path(os.environ['HARMONY_INPUT'])
    output = Path(os.environ['HARMONY_OUTPUT'])
    group = next(line.split('::', 1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines()
                 if line.startswith('0::'))
    cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
    report = dict(version=1, host=socket.gethostname(), attempt=os.environ['HARMONY_ATTEMPT'],
                  python=sys.version.split()[0], cpu_count=os.cpu_count(),
                  memory_max=(cgroup/'memory.max').read_text().strip(),
                  cpu_max=(cgroup/'cpu.max').read_text().strip(),
                  source_manifest_digest=hashlib.sha256((source/'.harmony-source.json').read_bytes()).hexdigest())
    (output/'probe.json').write_text(json.dumps(report, sort_keys=True)+'\n')
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
