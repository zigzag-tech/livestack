"""Optional installed fetcher for immutable inputs on a regional data path."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

from .model import WorkloadError


class InstalledInputMirror:
    """Run one trusted argv from worker config; job payloads cannot change it."""
    def __init__(self, config):
        if not isinstance(config, dict) or set(config) != {'argv', 'max_seconds'}:
            raise WorkloadError('invalid input mirror config')
        argv = config.get('argv')
        seconds = config.get('max_seconds')
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or
                any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in argv) or
                not Path(argv[0]).is_absolute() or isinstance(seconds, bool) or
                not isinstance(seconds, int) or not 1 <= seconds <= 1800):
            raise WorkloadError('invalid input mirror config')
        self.argv, self.max_seconds = tuple(argv), seconds

    def get(self, digest, destination, max_bytes):
        destination = Path(destination)
        if destination.exists():
            raise WorkloadError('mirror destination already exists')
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        os.close(fd)
        temporary = Path(temporary)
        try:
            result = subprocess.run([*self.argv, digest, str(temporary)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=self.max_seconds, check=False)
            if result.returncode:
                raise WorkloadError('input mirror unavailable', 503)
            if temporary.is_symlink() or not temporary.is_file() or temporary.stat().st_size > max_bytes:
                raise WorkloadError('input mirror returned invalid object', 503)
            hasher = hashlib.sha256()
            with temporary.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise WorkloadError('input mirror returned wrong digest', 503)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return destination
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkloadError('input mirror unavailable', 503) from error
        finally:
            temporary.unlink(missing_ok=True)
