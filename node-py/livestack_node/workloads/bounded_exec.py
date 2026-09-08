"""Installed execution wrapper: drain output into two bounded files.

Runs inside the attempt's systemd cgroup, including all descendants. The
supervisor must stop that cgroup even when the immediate command has exited.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import selectors
import time


def run(config_path):
    config = json.loads(Path(config_path).read_text())
    output = Path(config['output'])
    limit = int(config['log_bytes'])
    process = subprocess.Popen(config['argv'], cwd=config['cwd'], env=config['env'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = output/'command.log'
    stream = log.open('wb')
    size = 0
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pipe_open = True
    try:
        while True:
            # The lease is renewed by the outer supervisor, using this host's
            # monotonic clock. A dead supervisor cannot leave the job running.
            if config.get('lease_file'):
                try:
                    deadline = float(Path(config['lease_file']).read_text())
                except (OSError, ValueError):
                    raise SystemExit(75)
                if not time.monotonic() < deadline:
                    raise SystemExit(75)
            if not pipe_open and process.poll() is not None:
                break
            if not selector.select(timeout=.1):
                continue
            chunk = os.read(process.stdout.fileno(), min(65536, limit))
            if not chunk:
                selector.unregister(process.stdout)
                pipe_open = False
                continue
            if size + len(chunk) > limit:
                stream.close()
                os.replace(log, output/'command.previous.log')
                stream = log.open('wb')
                size = 0
            stream.write(chunk)
            stream.flush()
            size += len(chunk)
        code = process.wait()
        temporary = output/'exit.tmp'
        with temporary.open('w') as result:
            json.dump({'exit_code': code}, result)
            result.flush()
            os.fsync(result.fileno())
        os.replace(temporary, output/'exit.json')
    finally:
        selector.close()
        stream.close()
        process.stdout.close()


if __name__ == '__main__':
    run(sys.argv[1])
