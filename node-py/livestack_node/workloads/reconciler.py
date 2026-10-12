"""The rollout reconciler: observes drift always, and ENFORCES for the workers its config lists (rollout_exec.py).

Runs as its own process under its own `rollout` principal (owner decision 2026-10-08: smaller blast
radius than running inside the authority tick). It reads `GET rollout` and `GET workers`, runs the pure
planner (rollout.plan) and

  * posts one bounded observation to `POST rollout/report` (read back by `GET rollout`), and
  * appends what it WOULD do to its own action ledger when that intention changes.

Without an `enforce` block in its config it has no code path that drains, enables, stages, restarts or smokes
anything. With one (see rollout_exec.py) it also runs the executor for the listed groups, one worker at a time.
The authority's spec mode stays `observe` (spec mode `enforce` is refused by the authority and, if one is ever
read, ignored here with a logged `enforce_not_implemented`); enforcement is this process's own, operator-owned
config. Mode `off` in the spec makes it report states and stop any executor step safely.

    python -m livestack_node.workloads.reconciler --config reconciler.json [--once]

config: {"authority": "http://host:port", "token": "<rollout principal token>", "ledger": "path.jsonl",
         "interval_seconds": 30,
         "enforce": {"enabled": true, "kill_file": "...", "state": "...", "hosts": {"zz-joe": ["ubuntu@100.64.0.24"]},
                     "groups": [{"name": "...", "set": "<spec set>", "unit": "unit-xxxxxxxx", "workers": ["..."]}],
                     "notify_cmd": "...", "soak_seconds": 600, "failure_budget": 2}}
The `enforce` block is re-read every tick, so `"enabled": false` or the kill file takes effect at once.
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


def observe(client, ledger, previous_intent, now=None, fetched=None, applied=None, enforce=None):
    """One pass. Returns (report, intent). Raises WorkloadError when the authority cannot be read.
    `fetched` is (status, entries) already read this tick; `applied`/`enforce` are the executor's events and summary."""
    status, entries = fetched or (client.request('rollout'), client.roster()['workers'])
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
        unit_state=unit_state, drift=drift, would_do=would_do, applied=list(applied or [])[-50:],
        note='observe mode: nothing was changed' if enforce is None else 'enforce for listed groups only; everything else observe')
    if enforce is not None:
        report['enforce'] = enforce
    intent = json.dumps([would_do, drift], sort_keys=True)
    if intent != previous_intent:
        ledger.append('observe', mode=planned['mode'], spec_generation=status['spec_generation'],
                      would_do=would_do, drift=[d['kind'] + '@' + d['set'] for d in drift])
    client.request('rollout/report', dict(report=report))
    return report, intent


def enforce_config(path):
    """The live `enforce` block. Unreadable or absent means OFF (fail closed), never a guess."""
    try:
        block = json.loads(Path(path).read_text()).get('enforce')
    except (OSError, ValueError):
        return {}
    return block if isinstance(block, dict) else {}


def make_executor(client, ledger, config_path):
    block = enforce_config(config_path)
    if not block:
        return None
    from . import rollout_exec
    state = rollout_exec.StateFile(Path(block.get('state') or Path(config_path).with_name('rollout-executor.json')).expanduser())
    transport = rollout_exec.SshTransport({host: list(args) for host, args in (block.get('hosts') or {}).items()})
    return rollout_exec.Executor(client, transport, lambda: enforce_config(config_path), state, ledger)


def run_executor(executor, status, entries):
    """(events, summary) for the report; (None-safe) when no executor is configured. An executor error is logged
    and reported as `error`, and never stops the observation."""
    if executor is None:
        return [], None
    try:
        events = executor.tick(status, entries)
        state = executor.state_file.load()
        summary = dict(enabled=bool(executor.load_config().get('enabled')), active=state.get('active') and {
            k: state['active'][k] for k in ('worker', 'group', 'release', 'step')},
            paused=state['paused'], failures=state['failures'], waiting=executor.waiting)
        return events, summary
    except Exception as error:
        logging.error('enforce_failed: %s: %s', type(error).__name__, error)
        return [], dict(enabled=False, error=f'{type(error).__name__}: {str(error)[:160]}')


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
    executor = make_executor(client, ledger, args.config)
    while True:
        try:
            status = client.request('rollout')
            entries = client.roster()['workers']
            events, summary = run_executor(executor, status, entries)
            report, intent = observe(client, ledger, intent, fetched=(status, entries), applied=events, enforce=summary)
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
