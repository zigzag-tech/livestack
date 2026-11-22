# Process node: `units_probe` — announce only the units that can load

A Harmony process node (`livestack_node/process_node.py`) announces the `units` rows of its JSON
config. That list is hand-kept, and the GPU worker is not running when the node announces (it starts
lazily on a warm), so nothing checked it: node-g and node-h advertised polytts `cosyvoice`,
`dots` and `qwen` with no model files for them, and a broker would evict real work to warm a load
certain to fail.

## The key

```json
"units_probe": ["/home/ubuntu/polytts/venv-20261121/bin/python", "/home/ubuntu/polytts/engines.py"]
```

An argv run once at node startup (before it serves or announces), with the worker's `env` merged over
the facade's environment and the worker's `cwd`, under a 30 s timeout. It must print a JSON list of
unit kinds that can load on this host to stdout; anything on stderr is quoted in the log as the reason.

- A declared row the probe does not name is dropped and logged (`unit dots not offered: ...`).
  Footprints and every other row field still come from the config; the probe only removes rows.
- Probe failure (non-zero exit, timeout, not a JSON list of strings, cannot exec): the declared rows
  stand and a warning is logged. An unreadable probe is not evidence that a unit is absent, and it must
  not take the node down.
- `default_unit` dropped: the first surviving declared row (config order) becomes the default.
- No row survives: the node refuses to start (`process_node_units_probe_left_no_unit`). It has nothing
  to serve, so announcing nothing is the truth.
- The key is optional; without it nothing changes.

The facade knows nothing about any worker: the probe is the worker's own statement. For polytts it is
`python engines.py` (polytts README, "Only engines whose model files are on this host are offered"):
each engine's `missing()` checks its model offline, without importing torch, in ~0.2 s.

## Older releases

Deployed facades run their own base releases, not main's file. `node-py/scripts/backport-process-node-units-probe.py
<release>/process_node.py` copies main's `probe_units` into one, in place; the new release dir is the
first 16 hex of the patched file's sha256 (see `host-broker-release-deploy.md`).

## Checking it

`journalctl -u <unit>` at start shows each dropped unit, and `GET <broker>/fleet` → `hosts.<host>` lists
the units the node announced. To prove the probe works, add a row for an engine the host lacks,
restart while idle, and confirm it is not announced.
