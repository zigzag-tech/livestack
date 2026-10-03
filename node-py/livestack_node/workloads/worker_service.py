"""Run an unattended native worker; service managers restart this process."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import traceback

from .model import WorkloadError
from .worker import WorkloadWorker


class RemoteWorkloadFailed(RuntimeError):
    """Single-assignment remote invocation ended without Harmony success."""


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
        serve(worker, single_assignment=config.get('single_assignment', False),
              idle_timeout=config.get('idle_timeout_seconds', 300))
    finally:
        worker.close()


def serve(worker, sleep=time.sleep, *, single_assignment=False, idle_timeout=300):
    if type(single_assignment) is not bool or type(idle_timeout) not in (int,float) or not 1 <= idle_timeout <= 3600:
        raise WorkloadError('invalid single-assignment worker settings')
    seen = set()
    idle_deadline = time.monotonic()+idle_timeout
    while True:
        try:
            worked = worker.step()
            if worked and single_assignment:
                status = worker.client.request('worker/status', {})
                if status.get('state') == 'succeeded':
                    return
                if status.get('state') in ('failed','cancelled','expired'):
                    raise RemoteWorkloadFailed('remote workload ended '+str(status.get('state'))+': '+
                                               str(status.get('reason') or 'no reason'))
                idle_deadline = time.monotonic()+idle_timeout
            elif not worked and time.monotonic() >= idle_deadline:
                if single_assignment:
                    raise RemoteWorkloadFailed('remote workload was not claimed before its idle deadline')
            if not worked:
                sleep(2)
        except RemoteWorkloadFailed:
            raise
        except Exception as error:
            logging.warning('worker waiting after %s: %s', type(error).__name__, str(error)[:512])
            # The full traceback once per distinct type+location, so a wedge
            # names its own cause without repeating every 5 s.
            frame = traceback.extract_tb(error.__traceback__)[-1:]
            signature = (type(error).__name__, *((f.filename, f.lineno) for f in frame))
            if signature not in seen:
                seen.add(signature)
                logging.warning('worker wait cause (first occurrence):\n%s',
                                ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:])
            sleep(5)


if __name__ == '__main__':
    main()
