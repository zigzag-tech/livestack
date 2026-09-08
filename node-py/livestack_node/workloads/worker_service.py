"""Run an unattended native worker; service managers restart this process."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time

from .worker import WorkloadWorker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    root = Path(config['state_dir'])
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[
        RotatingFileHandler(root/'worker.log', maxBytes=8*1024**2, backupCount=2)])
    worker = WorkloadWorker(config)
    try:
        while True:
            try:
                if not worker.step():
                    time.sleep(2)
            except Exception as error:
                logging.warning('worker waiting after %s', type(error).__name__)
                time.sleep(5)
    finally:
        worker.close()


if __name__ == '__main__':
    main()
