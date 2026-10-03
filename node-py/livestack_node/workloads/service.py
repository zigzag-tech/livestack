"""Run `python -m livestack_node.workloads.service --config /path/config.json`."""
import argparse
import json
import logging
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .http import Principal, WorkloadServer, check_principals
from .model import Limits
from .store import WorkloadStore
from .blobs import BlobStore
from .artifact_mirror import InstalledArtifactMirror
from .compilation_policy import CompilationPolicy
from .github_remote import GitHubRemote


def load_principals(path):
    """Parse the config's principals with the startup rules. Raises ValueError."""
    try:
        config = json.loads(Path(path).read_text())
        principals = [Principal(**p) for p in config['principals']]
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f'principals unreadable: {type(exc).__name__}: {exc}') from exc
    check_principals(principals)
    return principals


def reload_principals(server, path, attempts=3, pause=.2):
    """Re-read `path` and swap the principal set; fail closed, keep the old one.

    A torn read (editor mid-write) is retried briefly before it counts. Returns
    True when a new set was applied. Never raises, never logs a token."""
    for attempt in range(attempts):
        try:
            new = load_principals(path)
            break
        except ValueError as exc:
            # JSONDecodeError is a ValueError: a torn write lands here.
            if attempt + 1 == attempts:
                logging.error('principal_reload_refused: %s; keeping the previous set', exc)
                return False
            time.sleep(pause)
    try:
        old = {p.id for p in server.principals}
        server.replace_principals(new)
    except ValueError as exc:
        logging.error('principal_reload_refused: %s; keeping the previous set', exc)
        return False
    ids = {p.id for p in new}
    logging.info('principal_reload_applied: %d principals, added=%s removed=%s',
                 len(new), sorted(ids-old), sorted(old-ids))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    root = Path(config['state_dir']).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One authority per database. A second instance must not clear readiness
    # under a live server by invoking recover().
    import fcntl
    with (root/'authority.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logging.basicConfig(level=logging.INFO, handlers=[
            RotatingFileHandler(root/'authority.log', maxBytes=16*1024*1024, backupCount=3)])
        principals = load_principals(args.config)
        github_remote = GitHubRemote(config['github_remote']) if 'github_remote' in config else None
        store = WorkloadStore(root/'workloads.sqlite', handlers=config['handlers'],
                              limits=Limits(**config.get('limits', {})),
                              environment_handlers=config.get('environment_handlers', {}),
                              compilation_policy=(CompilationPolicy(
                                  config.get('compilation_policy'), config['compilation_handlers'])
                                  if 'compilation_handlers' in config else None),
                              execution_providers=({} if github_remote is None else github_remote.handler_to_provider),
                              remote_hosts=({} if github_remote is None else github_remote.hosts))
        if github_remote is not None:
            if store.compilation_policy is None:
                raise ValueError('GitHub remote compilation requires the operator compilation policy')
            for provider_id, provider in github_remote.providers.items():
                for handler in provider.handlers:
                    if (not store.compilation_policy.required(handler) or
                            store.compilation_policy.handler_classes[handler] != tuple(sorted(
                                provider.config['compilation_classes']))):
                        raise ValueError('GitHub remote compilation classes differ from handler policy')
        store.recover()
        blobs = BlobStore(store, root/'objects', **config.get('blob_limits', {}))
        artifact_mirror = (InstalledArtifactMirror(config['artifact_mirror'])
                           if config.get('artifact_mirror') is not None else None)
        server = WorkloadServer((config.get('bind', '127.0.0.1'), config.get('port', 8802)),
                                store, principals, blobs=blobs, artifact_mirror=artifact_mirror,
                                github_remote=github_remote)
        server.blobs.recover()
        # SIGHUP re-reads the principals from --config (docs/authority-principal-reload.md).
        # A thread keeps the file read and its retries out of the signal handler.
        signal.signal(signal.SIGHUP, lambda *_: threading.Thread(
            target=reload_principals, args=(server, args.config), daemon=True).start())
        logging.info('workload authority started on %s', server.server_address)
        try:
            server.serve_forever(poll_interval=1)
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
