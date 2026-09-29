"""Run an unattended native worker; service managers restart this process."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import traceback

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
        serve(worker)
    finally:
        worker.close()


def serve(worker, sleep=time.sleep):
    seen = set()
    while True:
        try:
            if not worker.step():
                sleep(2)
        except Exception as error:
            logging.warning('worker waiting after %s: %s', type(error).__name__, str(error)[:512])
            # The full traceback once per distinct type+location, so a wedge
            # names its own cause without repeating every 5 s.
            frame = traceback.extract_tb(error.__traceback__)[-1:]
            signature = (type(error).__name__, *((f.filename, f.lineno) for f in frame))
            if signature not in seen:
                seen.add(signature)
                logging.warning('worker wait cause (first occurrence):\n%s',
                                ''.join(traceback.format_exception(error))[-4000:])
            sleep(5)


if __name__ == '__main__':
    main()
