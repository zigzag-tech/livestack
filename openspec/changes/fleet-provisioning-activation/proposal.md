## Why

`fleet-provisioning-operations` is archived: the durable lifecycle, the
supervision loop and the classifier rung are built, tested and merged. Two of
its tasks were not code, and archiving must not lose them.

Both are the same kind of thing — **handing a running process an authority it
does not yet have** — and neither is an engineering decision:

* No broker on this fleet has `LIVESTACK_FLEET_POOLS` set, so nothing has ever
  provisioned anything. Setting it, with real provider credentials, is what
  gives a live process the ability to rent machines. It is a spending decision.
* The Simple Jev rung runs in `shadow` and has no qualification receipt. A
  receipt is a *measurement*, and it needs a corpus that only a deployed
  lifecycle produces, plus labels somebody other than the incumbent confirmed.

The design record is `_plans/fleetd-weave-jev.md`; what is stale in it is its
status line, which will say IMPLEMENTED, NOT YET DEPLOYED until this change
lands. The prerequisites and exact commands are in `fleetd/receipts/README.md`.

A third thing surfaced while tracing what a quick `asr` request actually does:
**nothing feeds `fleetd` a queue.** `fleetTick` plans over a queue its caller
hands it, `POST /fleet/admit` answers one job at a time and keeps no queue, and
nothing joins the two. So a job the broker answers with `Queue` never reaches a
plan, and a burst that the scheduler would authorise never happens. The
lifecycle is built and the loop is built; the wire between them is not.

## What Changes

- Declare pools on the fleet broker and record the first real operations,
  their ledger joins, and evidence that the model path produced no effects.
- Build the incident corpus from those records, confirm its labels, run the
  balanced schedule against the live classifier, and publish a receipt.
- Close the admit→burst seam: decide who owns the queue of jobs the broker
  could not place, and give `fleetd` a way to read it.
- **Only then** consider promoting the classifier from `shadow` to `serve`,
  which is a separate explicit activation and is NOT granted by this change.

## Capabilities

- `fleet-provisioning-activation` (new): what must be true before a process may
  spend, and before a recorded selection may be acted on.

## Impact

`hostd`'s environment on the fleet broker host; `openspec/specs/` gains one
capability. The admit→burst seam is the one part that needs source, and its
shape is a real decision (see task 2.1) rather than a gap to be filled in
passing.

## Non-goals

Promoting the classifier to `serve`. Adding a second provider adapter. The
"no LAST_RESORT off-peak while over budget" hard rule, which
`fleet-provisioning-operations` deliberately did not adopt and which still
awaits an owner's decision.
