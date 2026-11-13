# Flash Next execution-cessation verification

This check runs against the real installed protected extraction endpoint. It
does not install a runtime or qualify model quality. Activate the matching
Strata and facade source first, with a private model/tokenizer-bound owner
waiver where internal quality acceptance is waived.

Run from the activated Livestack checkout, using its Python environment:

```sh
python tools/verify-extraction-cessation.py \
  --config /etc/harmony/pane-ask-runtime.json \
  --owner '<owner accepted by the configured service principal>' \
  --output /private/new-cessation-receipt.json
```

The script reads credentials only from the runtime's existing private files.
The configuration/profile/waiver must identify the Flash Next CUDA runtime;
the old 27B configuration refuses before admission. The private endpoint must
advertise its native GPU barrier and run in exclusive single-sequence mode.
Neither ordinary chat health nor a generated answer proves these conditions.

Each of three controls obtains its own normal Harmony fleet admission for the
registry target. There is no allocation bypass, unit edit or reservation override:

1. A short grounded question must complete with a correlated native GPU barrier.
2. Longer source evidence, with a 500 ms execution deadline, must return 504
   with the same execution identity, `execution_started: true` and
   `cessation_confirmed: true`. A refusal before execution or a normal fast
   completion is a failed control, not cancellation proof.
3. Another short question must complete, proving that confirmed cessation left
   the endpoint usable and the prior reservation was acknowledged as released.

`--cancel-budget-ms` accepts 100–2000 ms if hardware needs a different explicit
control budget; there is no automatic retry. The endpoint retains the serving
policy: temperature zero, thinking disabled, no tools and at most 1024 tokens.
The serving adapter's extraction instruction is reused.

An uncertain/uncorrelated response stops verification, retains the reservation
and records its lease ID. Never turn that failure into a release, restart or
repeated test without diagnosing it. If the endpoint quarantines uncertain
execution, its recovery must follow the normal runtime activation procedure.

The output is exclusive-create, mode 0600 and at most 64 KiB. It contains bounded
control metadata, immutable engine identity and lease/execution IDs, not bearer
tokens, prompts, generated text or response history. Each engine HTTP exchange
is bounded at 14 seconds; admission uses the existing eight-second budget and
release uses the normal one-second acknowledgement bound. At most three calls
are issued and the first failure stops the run.

A passing receipt proves the installed execution path's correlated completion
and deadline cessation for these controls. It is not an ASR evaluation, held-out
model-quality result, full Benchday suite verdict or release confirmation.
