## 1. Activate the lifecycle (operator)

- [ ] 1.1 Declare `LIVESTACK_FLEET_POOLS` on the fleet broker with one pool, a real
      price and a `max_instances` ceiling the owner has agreed to; set
      `LIVESTACK_FLEET_WORKER_ENV` to the broker address a machine in that region can
      actually reach. Verify: `GET /fleet` reports the pools and `adapter: true`.
      Ledger: the startup line names every pool, its price and its ceiling.
- [ ] 1.2 Provision one instance end to end. Verify: the node announces carrying its
      `LIVESTACK_OPERATION_ID`, the operation reaches `announced`, and one query by
      `job_id` joins the admit record, the lease and every operation transition.
      Ledger: `claim` + one `operation` record per transition.
- [ ] 1.3 Drain it. Verify: a busy node is refused with the lease count; an empty one
      reaches `released` and the instance is terminated. Ledger: the refusal is a
      record, not just an HTTP status.
- [ ] 1.4 Run `fleetd` against the broker for a period with the classifier in
      `shadow`. Verify: the green path shows zero escalations, and every recorded
      selection has `applied: false`.

## 2. Qualify the classifier rung

- [ ] 2.1 `scripts/incident-corpus.mts` against the broker's ledger. Verify: the
      report names the classes it is too thin to speak for, rather than a bare total.
- [ ] 2.2 Confirm labels with a person for at least the holdout, so no case is
      `agent_only`. Verify: jingway's evaluator no longer HOLDS the holdout.
- [ ] 2.3 `scripts/observe-incidents.mts` over the balanced schedule against the live
      classifier, then jingway's `scripts/evaluate-decisions.ts`. Verify: the report
      carries accepted-decision correctness, dangerous-action errors counted
      separately from misses, abstention and coverage, invariant rejections, order
      disagreement over distinct orderings, and full-cascade cost and p95 including
      the fallback.
- [ ] 2.4 Publish the receipt under `fleetd/receipts/`, and update
      `_plans/fleetd-weave-jev.md` to SHIPPED (shadow) with the numbers.

## 3. Verification before archive

- [ ] 3.1 Evidence for 1.2, 1.3 and 1.4 recorded in this change.
- [ ] 3.2 `openspec validate --specs` green.
