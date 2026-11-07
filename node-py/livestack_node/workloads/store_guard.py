"""Single model store per host, and the bounds on every per-worker scratch cache.

Rule: a host has ONE model/dependency cache (the shared Hugging Face hub store, e.g.
~/.cache/huggingface/hub). A worker's workspace and a job's environment never get their own
HF_HOME / HF_HUB_CACHE. Two parts, both opt-in through the worker config key `store_guard`:

  {"model_store": "/home/u/.cache/huggingface",   # HF_HOME of the host store (hub/ below it)
   "max_repo_bytes": 2147483648,                   # a model repo dir in a workspace above this is a violation
   "scan_seconds": 600,
   "sweep": {"incomplete_max_age_seconds": 86400,  # `*.incomplete` downloads older than this are removed
             "scratch_max_age_seconds": 86400,     # `flux2-offload-*` style scratch dirs older than this
             "scratch_cap_bytes": 21474836480}}    # newest-kept cap over all scratch dirs of this workspace

Guard (loud, never silent; docs/livestack-harmony.md "Single model store"): a `models--*`,
`datasets--*` or `spaces--*` directory inside the workspace that also exists in the host store
(`duplicate_of_host_store`) or exceeds `max_repo_bytes` (`over_bound`) is logged at ERROR as
`single_store_violation` and recorded in `<state_dir>/single-store-violations.json`. It is NOT
deleted: it may be a live download. The operator (or the sweep's age rule for scratch) decides.

Sweep (rule 10 bound + enforcer): removes only `*.incomplete` files and scratch dirs inside the
workspace (never the host store, never anything outside the workspace), oldest first beyond the cap.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import time

from .model import WorkloadError

REPO_PREFIXES = ('models--', 'datasets--', 'spaces--')
SCRATCH_PREFIXES = ('flux2-offload-',)
SKIP_DIRS = {'node_modules', '.git', 'target', '__pycache__', '.venv', 'venv'}
MAX_DEPTH = 6
MAX_ENTRIES = 200_000
GIB = 1024**3
KNOWN = ('model_store', 'max_repo_bytes', 'scan_seconds', 'sweep')
SWEEP_KNOWN = ('incomplete_max_age_seconds', 'scratch_max_age_seconds', 'scratch_cap_bytes')


def _positive(raw, key, default, where):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkloadError(f'{where}.{key}: Input should be a number > 0')
    return value


def tree_bytes(path, limit=MAX_ENTRIES):
    total = count = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            count += 1
            if count > limit:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def host_repos(store):
    """Repo directory names (`models--org--name`) present in the host store."""
    hub = Path(store)/'hub'
    try:
        return {p.name for p in hub.iterdir() if p.name.startswith(REPO_PREFIXES)}
    except OSError:
        return set()


def find_repo_dirs(root, *, max_depth=MAX_DEPTH, max_entries=MAX_ENTRIES):
    """HF repo directories under `root` (bounded depth and entry count; symlinks not followed)."""
    found, seen = [], 0
    stack = [(Path(root), 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return found
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith(REPO_PREFIXES):
                        found.append(Path(entry.path))
                    elif depth < max_depth and entry.name not in SKIP_DIRS:
                        stack.append((Path(entry.path), depth+1))
        except OSError:
            continue
    return found


class StoreGuard:
    def __init__(self, raw, *, workspace, state_dir, clock=time.time):
        if not isinstance(raw, dict):
            raise WorkloadError('store_guard: Input should be a valid dictionary')
        extra = [k for k in raw if k not in KNOWN]
        if extra:
            raise WorkloadError('; '.join(f'store_guard.{k}: Extra inputs are not permitted' for k in extra))
        store = raw.get('model_store')
        if not isinstance(store, str) or not os.path.isabs(store):
            raise WorkloadError('store_guard.model_store: Input should be an absolute path')
        self.store = Path(store)
        self.workspace = Path(workspace).resolve()
        if self.store.resolve() == self.workspace or self.workspace in self.store.resolve().parents:
            raise WorkloadError('store_guard.model_store: the host store must not be inside the workspace')
        self.max_repo_bytes = int(_positive(raw, 'max_repo_bytes', 2*GIB, 'store_guard'))
        self.scan_seconds = _positive(raw, 'scan_seconds', 600, 'store_guard')
        sweep = raw.get('sweep', {})
        if not isinstance(sweep, dict) or [k for k in sweep if k not in SWEEP_KNOWN]:
            raise WorkloadError('store_guard.sweep: Input should be a dictionary with known keys')
        self.incomplete_age = _positive(sweep, 'incomplete_max_age_seconds', 86400, 'store_guard.sweep')
        self.scratch_age = _positive(sweep, 'scratch_max_age_seconds', 86400, 'store_guard.sweep')
        self.scratch_cap = int(_positive(sweep, 'scratch_cap_bytes', 20*GIB, 'store_guard.sweep'))
        self.state_path = Path(state_dir)/'single-store-violations.json'
        self.clock = clock
        self._logged = set()
        self._next = 0.0
        self.violations = []

    @property
    def hub(self):
        return self.store/'hub'

    def job_env(self):
        """The only model-cache environment a workload job may have."""
        return dict(HF_HOME=str(self.store), HF_HUB_CACHE=str(self.hub), HF_HUB_OFFLINE='1')

    def check_environment(self, environment):
        """Fail startup when the worker's own job environment points a cache at the workspace."""
        for key in ('HF_HOME', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'TRANSFORMERS_CACHE'):
            value = (environment or {}).get(key)
            if value is None:
                continue
            resolved = Path(value).resolve()
            if resolved == self.workspace or self.workspace in resolved.parents:
                raise WorkloadError(f'single_store_violation: environment.{key} puts a model cache inside the '
                                    'workspace; the host store is the only model cache', 500)

    def scan(self, root=None):
        """Violations under `root` (default: the whole workspace). Logs each new one at ERROR."""
        host = host_repos(self.store)
        found = []
        for path in find_repo_dirs(root or self.workspace):
            size = tree_bytes(path)
            kinds = []
            if path.name in host:
                kinds.append('duplicate_of_host_store')
            if size > self.max_repo_bytes:
                kinds.append('over_bound')
            for kind in kinds:
                found.append(dict(path=str(path), repo=path.name, kind=kind, bytes=size))
        for item in found:
            key = (item['path'], item['kind'])
            if key not in self._logged:
                self._logged.add(key)
                logging.error('single_store_violation: kind=%s repo=%s bytes=%d path=%s (host store %s is the only '
                              'model cache)', item['kind'], item['repo'], item['bytes'], item['path'], self.store)
        if root is None:
            self.violations = found
            self._persist()
        return found

    def _persist(self):
        try:
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(dict(at=self.clock(), violations=self.violations[:64]), sort_keys=True))
            os.replace(tmp, self.state_path)
        except OSError as error:
            logging.error('single_store_violation_state_unwritable: %s: %s', type(error).__name__, error)

    def _inside_workspace(self, path):
        real = Path(path).resolve()
        return self.workspace in real.parents

    def sweep(self):
        """Remove stale `*.incomplete` and over-cap/old scratch dirs inside the workspace only."""
        now = self.clock()
        removed = dict(incomplete=0, scratch=0, bytes=0)
        # Direct and one-level-down scratch/hub dirs only: bounded and predictable.
        scratch = []
        for entry in _scandir(self.workspace):
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith(SCRATCH_PREFIXES):
                scratch.append(entry)
        for path in _incomplete_files(self.workspace):
            try:
                if now-path.lstat().st_mtime > self.incomplete_age and self._inside_workspace(path):
                    size = path.lstat().st_size
                    path.unlink()
                    removed['incomplete'] += 1
                    removed['bytes'] += size
            except OSError as error:
                logging.warning('store_sweep: cannot remove %s: %s', path, error)
        sized = []
        for entry in scratch:
            try:
                sized.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
            except OSError:
                pass
        sized.sort(reverse=True)  # newest first
        kept = 0
        for mtime, path in sized:
            size = tree_bytes(path)
            old = now-mtime > self.scratch_age
            if old or kept+size > self.scratch_cap:
                if self._inside_workspace(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed['scratch'] += 1
                    removed['bytes'] += size
            else:
                kept += size
        if removed['incomplete'] or removed['scratch']:
            logging.info('store_sweep: removed %d incomplete files and %d scratch dirs (%d bytes)',
                         removed['incomplete'], removed['scratch'], removed['bytes'])
        return removed

    def tick(self):
        """Periodic scan and sweep; called from the worker step. Never raises into the worker."""
        if self.clock() < self._next:
            return
        self._next = self.clock()+self.scan_seconds
        try:
            self.sweep()
            self.scan()
        except Exception as error:
            logging.error('single_store_guard_failed: %s: %s', type(error).__name__, str(error)[:512])


def _scandir(path):
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _incomplete_files(root, max_depth=MAX_DEPTH):
    seen = 0
    stack = [(Path(root), 0)]
    while stack:
        directory, depth = stack.pop()
        for entry in _scandir(directory):
            seen += 1
            if seen > MAX_ENTRIES:
                return
            if entry.is_dir(follow_symlinks=False):
                if depth < max_depth and entry.name not in SKIP_DIRS:
                    stack.append((Path(entry.path), depth+1))
            elif entry.name.endswith('.incomplete') and entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
