"""Installed execution wrapper: drain output into two bounded files.

Linux: runs inside the attempt's systemd cgroup, including all descendants. The
supervisor must stop that cgroup even when the immediate command has exited.

macOS: runs as the attempt's launchd job, which has no cgroup, so this wrapper
also enforces the limits launchd holds in its argv (`--memory-bytes`, `--tasks`,
`--max-seconds`): see DarwinLimits.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import selectors
import time

if sys.platform == 'darwin':
    import darwin_proc
else:
    from resource_usage import resource_usage

# One sample of the tree every half second: a spike shorter than that can
# overshoot the memory cap (design "Limits").
DARWIN_SAMPLE_SECONDS = .5


class DarwinLimits:
    """Memory (physical footprint of the tree), task count and wall time,
    enforced by kill, recorded in the receipt fields the Linux cgroup fills."""

    def __init__(self, memory_bytes, tasks, max_seconds):
        self.memory_bytes, self.tasks, self.max_seconds = memory_bytes, tasks, max_seconds
        self.started = time.monotonic()
        me = darwin_proc.info(os.getpid())
        self.root = (me['pid'], me['start'])
        self.seen = {}
        self.memory_peak = 0
        self.tasks_peak = 0
        self.oom_kill = 0
        self.pids_max_events = 0
        self.next_sample = 0

    def check(self):
        """None while within limits, else the breach that killed the tree."""
        now = time.monotonic()
        if now < self.next_sample:
            return None
        self.next_sample = now+DARWIN_SAMPLE_SECONDS
        found = darwin_proc.tree(*self.root)
        for pid, record in found.items():
            if len(self.seen) < darwin_proc.MAX_TREE:
                self.seen.setdefault(pid, record)
        footprint = sum(darwin_proc.footprint(p) or 0 for p in found)
        self.memory_peak = max(self.memory_peak, footprint)
        self.tasks_peak = max(self.tasks_peak, len(found))
        breach = None
        if footprint > self.memory_bytes:
            self.oom_kill, breach = 1, 'memory'
        elif len(found) > self.tasks:
            self.pids_max_events, breach = 1, 'tasks'
        elif now-self.started > self.max_seconds:
            breach = 'wall time'
        if breach:
            self.kill()
        return breach

    def kill(self):
        """Everything this attempt ever started that still lives, except us."""
        victims = {p: r for p, r in darwin_proc.survivors(self.seen).items() if p != os.getpid()}
        for pid, record in darwin_proc.tree(*self.root).items():
            if pid != os.getpid():
                victims.setdefault(pid, record)
        darwin_proc.kill_tree(victims, signal.SIGKILL)

    def resources(self):
        usage = __import__('resource').getrusage(__import__('resource').RUSAGE_CHILDREN)
        return dict(memory_peak_bytes=self.memory_peak, tasks_peak=self.tasks_peak, oom_kill=self.oom_kill,
                    pids_max_events=self.pids_max_events,
                    cpu_usage_usec=int((usage.ru_utime+usage.ru_stime)*1e6))


def run(config_path, limits=None):
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
            if limits is not None:
                # A breach kills the tree, including the command; the loop
                # then drains what it wrote and records the receipt.
                limits.check()
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
        if limits is not None:
            limits.kill()  # before the receipt: a finished attempt has no survivors
        resources = limits.resources() if limits is not None else resource_usage()
        temporary = output/'exit.tmp'
        with temporary.open('w') as result:
            json.dump({'exit_code': code, 'resources': resources}, result)
            result.flush()
            os.fsync(result.fileno())
        os.replace(temporary, output/'exit.json')
    finally:
        if limits is not None:
            # A descendant that outlived the command (reparented to launchd)
            # dies with the attempt, as a cgroup's would; so does everything
            # when the lease lapses (SystemExit 75 above).
            limits.kill()
        selector.close()
        stream.close()
        process.stdout.close()


def main(argv):
    if sys.platform != 'darwin':
        run(argv[0])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('--memory-bytes', type=int, required=True)
    parser.add_argument('--cpu', type=float, required=True)
    parser.add_argument('--tasks', type=int, required=True)
    parser.add_argument('--max-seconds', type=int, required=True)
    args = parser.parse_args(argv)
    run(args.config, DarwinLimits(args.memory_bytes, args.tasks, args.max_seconds))


if __name__ == '__main__':
    main(sys.argv[1:])
