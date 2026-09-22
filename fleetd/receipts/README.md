# Receipts for `fleet.failure_class`

**There is no qualification receipt here, and that is the accurate state.**

A receipt is a measurement, not a document. Producing one requires three things
that do not exist yet on this fleet:

1. **A corpus.** `scripts/incident-corpus.mts` captures one from a broker's
   decision ledger, but a broker only writes `operation` records once it is
   actually provisioning. Until the deterministic lifecycle has run on real
   hardware for a while, the ledger holds nothing to freeze.
2. **Labels somebody other than the incumbent produced.** The capture marks a
   label taken from the broker's own `error.class` as `agent_only`, and
   jingway's evaluator HOLDS a holdout containing one rather than qualifying it.
   That is correct: a receipt built on the incumbent's opinion measures
   agreement, not correctness. Classes need a person to confirm them.
3. **A live classifier.** `scripts/observe-incidents.mts` runs the frozen cases
   over a balanced permutation schedule against Harmony's `/v1/classifier`.

Then, and only then:

```bash
npx tsx scripts/incident-corpus.mts   --broker http://100.64.0.18:8801 --out receipts/cases.json
npx tsx scripts/observe-incidents.mts --cases receipts/cases.json \
    --classifier http://100.64.0.18:8188 --split development --out receipts/observations.json
npx tsx ~/jingway/scripts/evaluate-decisions.ts --cases receipts/cases.json \
    --baseline receipts/incumbent.json --candidate receipts/observations.json \
    --schedule-seed fleet-incident-seed-1
```

## What the receipt has to report before `serve` is even discussable

Accepted-decision correctness; **dangerous-action errors** counted separately
from misses (a class a code invariant rejects is not a near miss);
abstention and coverage; invariant rejections; order disagreement over
*distinct* orderings; and full-cascade cost and p95 latency **including the
fallback** — an arm that forwards every hard case to a person cannot qualify on
the price of the easy ones.

## And `serve` is still not this change

Promotion is a separate, explicitly approved activation. `shadow` is what this
change ships: the selection is recorded with its order, its probabilities and
the workflow it *would* have run, while nothing depends on it being right.
