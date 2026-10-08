"""The rollout reconciler, OBSERVE mode only: compares desired state with the fleet and reports drift.

Runs as its own process under its own `rollout` principal (owner decision 2026-10-08: smaller blast
radius than running inside the authority tick). It reads `GET rollout` and `GET workers`, runs the pure
planner (rollout.plan) and

  * posts one bounded observation to `POST rollout/report` (read back by `GET rollout`), and
  * appends what it WOULD do to its own action ledger when that intention changes.

It has no code path that drains, enables, stages, restarts or smokes anything: there is no executor in
this release, and spec mode `enforce` is refused by the authority and, if one is ever read, ignored here
with a logged `enforce_not_implemented`. Mode `off` in the spec makes it report states and nothing else.

    python -m livestack_node.workloads.reconciler --config reconciler.json [--once]

config: {"authority": "http://host:port", "token": "<rollout principal token>", "ledger": "path.jsonl",
         "interval_seconds": 30}
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import time
from pathlib import Path

from .claims import ActionLedger
from .client import WorkloadClient
from . import rollout

MIN_INTERVAL = 10


def observe(client, ledger, previous_intent, now=None):
    """One pass. Returns (report, intent). Raises WorkloadError when the authority cannot be read."""
    status = client.request('rollout')
    entries = client.roster()['workers']
    spec = status['spec']
    mode = status['mode']
    if mode == 'enforce':
        logging.error('enforce_not_implemented: this reconciler only observes; spec mode ignored')
        spec = dict(spec, mode='observe')
    hour = (datetime.datetime.fromtimestamp(now, datetime.timezone.utc).hour if now is not None
            else datetime.datetime.now(datetime.timezone.utc).hour)
    planned = rollout.plan(spec, entries, status['manifests'], hour_utc=hour)
    would_do = [dict(set=name, **action) for name, body in sorted(planned['sets'].items())
                for action in body['actions']]
    drift = [dict(set=name, **item) for name, body in sorted(planned['sets'].items()) for item in body['drift']]
    unit_state = {worker: info['unit_state'] for worker, info in sorted(planned['workers'].items())}
    report = dict(
        observed_at=round(time.time(), 3), mode=planned['mode'], spec_generation=status['spec_generation'],
        sets={name: dict(unit=body['unit'], workers=body['workers'], waiting=body['waiting'],
                         states={w: s['unit_state'] + (f"/{s['skipped']}" if s['skipped'] else '')
                                 for w, s in body['states'].items()}) for name, body in planned['sets'].items()},
        unit_state=unit_state, drift=drift, would_do=would_do, applied=[], note='observe mode: nothing was changed')
    intent = json.dumps([would_do, drift], sort_keys=True)
    if intent != previous_intent:
        ledger.append('observe', mode=planned['mode'], spec_generation=status['spec_generation'],
                      would_do=would_do, drift=[d['kind'] + '@' + d['set'] for d in drift])
    client.request('rollout/report', dict(report=report))
    return report, intent


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    client = WorkloadClient(config['authority'], config['token'], timeout=30)
    ledger = ActionLedger(config['ledger'])
    interval = max(MIN_INTERVAL, int(config.get('interval_seconds', 30)))
    intent = None
    while True:
        try:
            report, intent = observe(client, ledger, intent)
            logging.info('observed: mode=%s sets=%d drift=%d would_do=%d', report['mode'],
                         len(report['sets']), len(report['drift']), len(report['would_do']))
        except Exception as error:  # a dead authority means no observation, never a crash loop that hammers it
            logging.error('observe_failed: %s: %s', type(error).__name__, error)
            failed = True
        else:
            failed = False
        if args.once:
            return 1 if failed else 0
        time.sleep(interval)


if __name__ == '__main__':
    raise SystemExit(main())
