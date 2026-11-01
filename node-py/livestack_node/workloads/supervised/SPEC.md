# Supervised handlers (one spec, two implementations)

`supervised.py` and `supervised.mjs` implement exactly this file. `vectors.json` is the shared test
vector file both must reproduce byte for byte (`tests/test_workload_supervised.py` runs both).

## Why

Progress that depends on a handler remembering to report is a convention. A supervised handler cannot
do slow work except through a `ctx`, and the `ctx` reports on its behalf. A conformance scan
(`tools/check-supervised-handlers.py`) fails any handler module that imports a raw process, network or
sleep primitive, so the escape hatch does not exist in handler code.

## Handler API

    supervised(contract, mode="report", clock=..., backend=..., sink=...)(handler)   # handler(ctx, *args)

    ctx.phase(name, fn)                  start phase `name`, run fn(), return its result
    ctx.exec(cmd, args, env=, cwd=)      spawn; every output chunk and the exit tick; returns {code, tail}
    ctx.each(items, fn, unit=)           total=len(items), done after each fn(item)
    ctx.wait(awaitable, label)           await/join one external wait; the label becomes the message
    ctx.download(url, dest)              stream to a file, a tick per chunk, bytes as done/total
    ctx.upload(url, path)                stream a file, a tick per chunk
    ctx.tick(message=None)               explicit liveness (for the rare computation inside a phase)

`mode="report"` (default; `HARMONY_SUPERVISED_MODE` overrides) logs a silence breach and carries on.
`mode="enforce"` kills the child of `ctx.exec` and raises `SilenceBreach` (Python) / rejects with it (Node).

## Contract

`{max_silence_seconds (30..86400), min_interval_seconds (0..3600, default 1),
  max_per_period: {count, seconds} (default 6 per 60)}`; the same object a handler release declares
as `progress_contract` at enrolment.

## Reducer (pure; time is an argument, in integer milliseconds)

State: `doc` (wanted fields), `beat`, `emitted` (last emitted document), `last_event`, `last_emit`,
`milli_base`, `base_t`, `dropped`, `pending`.

`init(contract, t)`: doc = {phase: "start"} (published only if a tick/done arrives before any phase), milli_base = count*1000, base_t = last_event = t, everything else empty.
`tokens(t) = min(count*1000, milli_base + (t - base_t) * count // seconds)` (milli-tokens, integer only).

`step(state, event)` with `event.t`:
1. (no refill step: `tokens(t)` is computed on demand, so integer flooring never accumulates)
2. apply (not for `flush`): `phase` -> doc = {phase}; `done` -> doc.done/total/unit (+ message if
   given); `tick` -> beat += 1 (+ message if given). `last_event = t` for all but `flush`.
3. wanted = doc plus `beat` when beat > 0. Equal to `emitted`: pending = false, no emit.
4. Else if `tokens(t) >= 1000` and (`last_emit` empty or `t - last_emit >= min_interval_seconds*1000`):
   emit wanted, spend: `milli_base = tokens(t) - 1000; base_t = t`, `last_emit = t`, `emitted = wanted`, pending = false.
5. Else pending = true, and `dropped += 1` unless the event is `flush`.

`verdict(state, t)`: `silent_ms = t - last_event`; `overdue` when `silent_ms > max_silence_seconds*1000`.

An emitted document is `dropped`-free: `dropped` is reported by the authority from its own bucket and by
the wrapper's exit log, never inside the document.

## Vectors

`vectors.json`: `[{name, contract, events:[{t,type,...}], expect:{emits:[{t,doc}], dropped, verdicts:[{t,state}]}}]`
where `verdicts` are evaluated at the listed times after all events.
