"""Run `python -m livestack_node.workloads.service --config /path/config.json`."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .http import Principal, WorkloadServer
from .model import Limits
from .store import WorkloadStore
from .blobs import BlobStore


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
        principals = [Principal(**p) for p in config['principals']]
        store = WorkloadStore(root/'workloads.sqlite', handlers=config['handlers'],
                              limits=Limits(**config.get('limits', {})))
        store.recover()
        blobs = BlobStore(store, root/'objects', **config.get('blob_limits', {}))
        server = WorkloadServer((config.get('bind', '127.0.0.1'), config.get('port', 8802)),
                                store, principals, blobs=blobs)
        server.blobs.recover()
        logging.info('workload authority started on %s', server.server_address)
        try:
            server.serve_forever(poll_interval=1)
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
