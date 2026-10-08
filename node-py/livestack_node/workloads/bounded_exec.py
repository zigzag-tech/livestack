"""Installed execution wrapper: drain output into two bounded files.

Linux: runs inside the attempt's systemd cgroup, including all descendants. The
supervisor must stop that cgroup even when the immediate command has exited.

macOS: runs as the attempt's launchd job, which has no cgroup, so this wrapper
also enforces the limits launchd holds in its argv (`--memory-bytes`, `--tasks`,
`--max-seconds`): see DarwinLimits.

Windows: runs inside the attempt's Job Object, which the kernel bounds (commit,
process count, CPU rate); this wrapper turns the job's limit notifications into
the Linux receipt fields and enforces wall time: see WindowsLimits.
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
import threading
import queue
import time

if sys.platform == 'darwin':
    import darwin_proc
elif sys.platform == 'win32':
    import windows_proc
else:
    from resource_usage import resource_usage, filesystem_used

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


class WindowsLimits:
    """The job's kernel limits, observed: a memory-limit notification kills the
    rest of the job (as cgroup OOMPolicy=kill does) and records oom_kill; a
    process-limit notification records pids_max_events (a refused spawn, as on
    Linux); wall time is enforced here."""

    def __init__(self, job_name, max_seconds):
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self.job = windows_proc.Job.open(job_name)
        # Holding this handle keeps the job, and so its name, alive across a
        # worker restart; the worker created it with kill-on-close.
        if self.job is None or not self.job.contains(windows_proc._k32().GetCurrentProcess()):
            raise SystemExit(75)
        self.port = self.job.watch()
        self.oom_kill = 0
        self.pids_max_events = 0

    def check(self):
        messages = windows_proc.limit_messages(self.port)
        breach = None
        if windows_proc.JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT in messages:
            self.pids_max_events = 1
        if windows_proc.JOB_OBJECT_MSG_JOB_MEMORY_LIMIT in messages:
            self.oom_kill, breach = 1, 'memory'
        elif time.monotonic()-self.started > self.max_seconds:
            breach = 'wall time'
        if breach:
            self.kill()
        return breach

    def kill(self):
        # The parent too: a venv's python.exe is a launcher that runs the real
        # interpreter as its child and dies with it (kill-on-close job).
        if not self.job.kill_others({os.getpid(), os.getppid()}):
            # Something keeps respawning: end the whole job, this wrapper too.
            # No receipt means the worker reports an infrastructure stop.
            self.job.terminate()

    def resources(self):
        limits, accounting = self.job.limits(), self.job.accounting()
        return dict(memory_peak_bytes=limits['peak_memory_bytes'], tasks_peak=accounting['total'],
                    oom_kill=self.oom_kill, pids_max_events=self.pids_max_events,
                    cpu_usage_usec=accounting['cpu_usage_usec'])


class _PipeReader:
    """Windows select() takes sockets only: read the pipe on a thread."""

    def __init__(self, pipe, chunk):
        self.chunks = queue.Queue(maxsize=64)
        self.thread = threading.Thread(target=self._read, args=(pipe, chunk), daemon=True)
        self.thread.start()

    def _read(self, pipe, chunk):
        while True:
            data = os.read(pipe.fileno(), chunk)
            self.chunks.put(data)
            if not data:
                return

    def read(self, timeout):
        """bytes, b'' at end of stream, or None when nothing arrived."""
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None


def run(config_path, limits=None):
    config = json.loads(Path(config_path).read_text())
    output = Path(config['output'])
    limit = int(config['log_bytes'])
    cwd_path = config['cwd']
    # Workspace filesystem baseline for the receipt's disk delta (Linux wrapper only).
    disk_baseline = filesystem_used(cwd_path) if limits is None and sys.platform not in ('darwin', 'win32') else None
    process = subprocess.Popen(config['argv'], cwd=config['cwd'], env=config['env'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = output/'command.log'
    stream = log.open('wb')
    size = 0
    if sys.platform == 'win32':
        selector, reader = None, _PipeReader(process.stdout, min(65536, limit))
    else:
        reader, selector = None, selectors.DefaultSelector()
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
            if not pipe_open:
                time.sleep(.1)
                continue
            if reader is not None:
                chunk = reader.read(.1)
                if chunk is None:
                    continue
            else:
                if not selector.select(timeout=.1):
                    continue
                chunk = os.read(process.stdout.fileno(), min(65536, limit))
            if not chunk:
                if selector is not None:
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
        resources = limits.resources() if limits is not None else resource_usage(cwd_path, disk_baseline)
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
        if selector is not None:
            selector.close()
        stream.close()
        process.stdout.close()


def main(argv):
    if sys.platform == 'win32':
        parser = argparse.ArgumentParser()
        parser.add_argument('config')
        parser.add_argument('--job', required=True)
        parser.add_argument('--max-seconds', type=int, required=True)
        args = parser.parse_args(argv)
        run(args.config, WindowsLimits(args.job, args.max_seconds))
        return
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
