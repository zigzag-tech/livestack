"""Run `python -m livestack_node.workloads.service --config /path/config.json`."""
import argparse
import json
import logging
import signal
import socket
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ReloadableConfig, load_config
from .handler_registry import parse_policy
from .http import Principal, WorkloadServer, check_grant_origins, check_principals
from .model import Limits
from .store import WorkloadStore
from .blobs import BlobStore
from .artifact_mirror import InstalledArtifactMirror
from .compilation_policy import CompilationPolicy
from ..ledger import JsonlLedger


_UNCHANGED = object()


def apply_storage_policy(server, config):
    """(Re)build the headroom guard and retention tiers from the validated config sections
    and install them. Absent sections install None: today's flat behaviour."""
    from .retention_tiers import RetentionTiers
    from .storage_bounds import HeadroomGuard, StorageBounds
    bounds = StorageBounds.validate(config['storage_bounds']) if config.get('storage_bounds') is not None else None
    tiers = RetentionTiers.validate(config['retention_tiers']) if config.get('retention_tiers') is not None else None
    blobs = server.blobs
    guard = HeadroomGuard(blobs.root, blobs.max_bytes, bounds) if bounds is not None else None
    blobs.replace_policy(guard, tiers, bounds.gc_batch if bounds is not None else 256)
    server.store.retention_tiers = tiers
    if guard is not None:
        guard.snapshot()  # logs the effective bound at startup/reload
    return guard, tiers


def load_reloadable(path):
    """Parse the reloadable sections with the startup rules. Raises ValueError.
    Pure: nothing is applied until every section has passed."""
    try:
        config = load_config(path, ReloadableConfig)
        principals = [Principal(**p) for p in config['principals']]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f'principals unreadable: {type(exc).__name__}: {exc}') from exc
    check_principals(principals)
    environment_handlers = config.get('environment_handlers', _UNCHANGED)
    return principals, config['handlers'], config.get('handler_release_policy'), environment_handlers


def load_principals(path):
    return load_reloadable(path)[0]


def reload_principals(server, path, attempts=3, pause=.2):
    """Re-read `path` and swap reloadable authority policy without a restart.

    A torn read (editor mid-write) is retried briefly before it counts. Returns
    True when the config was applied. An omitted environment_handlers section
    preserves the installed environment policy; an explicit empty mapping
    disables environment enrollment. Never raises, never logs a token."""
    for attempt in range(attempts):
        try:
            new, handlers, policy, environment_handlers = load_reloadable(path)
            break
        except ValueError as exc:
            # JSONDecodeError is a ValueError: a torn write lands here.
            if attempt + 1 == attempts:
                logging.error('principal_reload_refused: %s; keeping the previous set', exc)
                return False
            time.sleep(pause)
    store, registry = server.store, server.handler_registry
    try:
        removed = store.handlers - set(handlers)
        if removed:
            raise ValueError('handler_removal_refused: ' + ', '.join(sorted(removed))
                             + ' cannot be removed without a restart')
        all_handlers = store.handlers | set(handlers)
        parse_policy(policy, all_handlers)
        parsed_environment_handlers = (
            store.parse_environment_handlers(environment_handlers, all_handlers)
            if environment_handlers is not _UNCHANGED else None)
        old = {p.id for p in server.principals}
        server.replace_principals(new)
    except ValueError as exc:
        logging.error('principal_reload_refused: %s; keeping the previous set', exc)
        return False
    added = sorted(all_handlers - store.handlers)
    store.add_handlers(handlers)
    registry.replace_policy(policy, store.handlers)
    if environment_handlers is not _UNCHANGED:
        store.environment_handlers = parsed_environment_handlers
    ids = {p.id for p in new}
    environment_count = ('unchanged' if environment_handlers is _UNCHANGED
                         else len(parsed_environment_handlers))
    logging.info('principal_reload_applied: %d principals, added=%s removed=%s; handlers added=%s; '
                 'environment_handlers=%s',
                 len(new), sorted(ids-old), sorted(old-ids), added, environment_count)
    try:
        apply_storage_policy(server, load_config(path, ReloadableConfig))
    except ValueError as exc:
        logging.error('storage_policy_reload_refused: %s; keeping the previous policy', exc)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
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
        check_grant_origins(principals, config.get('public_base_url'))
        if 'github_remote' in config:
            # GitHub dispatch is optional. Keep its third-party crypto
            # dependency out of authorities that do not configure that
            # provider (including the source-only Harmony test runtime).
            from .github_remote import GitHubRemote
            github_remote = GitHubRemote(config['github_remote'])
        else:
            github_remote = None
        decision_ledger = JsonlLedger(
            str(root/'workload-decisions.jsonl'), max_bytes=64*1024*1024, max_files=4,
            log=lambda message: logging.warning('%s', message))
        store = WorkloadStore(root/'workloads.sqlite', handlers=config['handlers'],
                              limits=Limits(**config.get('limits', {})),
                              environment_handlers=config.get('environment_handlers', {}),
                              compilation_policy=(CompilationPolicy(
                                  config.get('compilation_policy'), config['compilation_handlers'])
                                  if 'compilation_handlers' in config else None),
                              execution_providers=({} if github_remote is None else github_remote.handler_to_provider),
                              remote_hosts=({} if github_remote is None else github_remote.hosts),
                              decision_ledger=decision_ledger,
                              decision_emitter_id=config.get('identity_authority_id') or socket.gethostname())
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
                                github_remote=github_remote,
                                handler_release_policy=config.get('handler_release_policy'),
                                public_base_url=config.get('public_base_url'),
                                identity_authority_id=config.get('identity_authority_id'))
        server.blobs.recover()
        apply_storage_policy(server, config)
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
