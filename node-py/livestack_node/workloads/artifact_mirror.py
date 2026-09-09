"""Optional installed uploader for immutable artifacts on a regional data path."""
import hashlib
import os
from pathlib import Path
import subprocess

from .model import WorkloadError


class InstalledArtifactMirror:
    """Run one trusted argv from worker config; job payloads cannot change it."""
    def __init__(self, config):
        if not isinstance(config, dict) or set(config) != {'argv', 'max_seconds'}:
            raise WorkloadError('invalid artifact mirror config')
        argv, seconds = config.get('argv'), config.get('max_seconds')
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or
                any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in argv) or
                not Path(argv[0]).is_absolute() or isinstance(seconds, bool) or
                not isinstance(seconds, int) or not 1 <= seconds <= 1800):
            raise WorkloadError('invalid artifact mirror config')
        self.argv, self.max_seconds = tuple(argv), seconds

    def put(self, digest, source, max_bytes):
        source = Path(source)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > max_bytes:
            raise WorkloadError('artifact mirror source is invalid')
        before = source.stat()
        hasher = hashlib.sha256()
        with source.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise WorkloadError('artifact mirror source has wrong digest')
        try:
            result = subprocess.run([*self.argv, digest, str(source)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.max_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkloadError('artifact mirror unavailable', 503) from error
        after = source.stat()
        if result.returncode or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise WorkloadError('artifact mirror unavailable', 503)

