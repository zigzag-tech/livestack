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

## 2. Close the admit→burst seam

- [x] 2.1 **Decided 2026-09-22: none of the three — a decaying demand SIGNAL, not a
      queue.** The three shapes offered were (a) `hostd` retains queued jobs, (b)
      `/fleet/plan` reads them, (c) callers re-present their own. (a) and (b) share a
      defect the option list did not name: a retained queue can hold work whose caller
      gave up ten minutes ago, and provisioning for it spends money for nothing. (c)
      has no new broker state but requires every caller to implement a queue, and none
      do.
      `fleet_demand.DemandRegister` takes the useful half of (a) without the defect:
      `/fleet/admit` records a CAPACITY refusal (never a quota refusal — an account at
      its ceiling does not need a bigger fleet) into a bounded, TTL'd, in-memory
      register. Demand must be CURRENT to justify spending, so a caller that stops
      retrying stops counting within the TTL, and the existing retry behaviour of
      attune and media-corpus is what keeps a live signal alive. In memory on purpose:
      a restart forgetting it is correct, because demand older than the restart is not
      demand.
- [x] 2.2 Implemented: `node-py/livestack_node/fleet_demand.py`, recorded in the
      `/fleet/admit` route, consumed by `/fleet/plan` (`include_demand`, default true),
      reported on `GET /fleet`. One job per SHAPE, never one per refusal — turning a
      refusal count into a job count is how a brief spike rents a datacentre. Tests:
      `tests/test_fleet_demand.py` (13) — it forgets, it is bounded, it does not
      inflate, and demand reaches a plan and produces a `provision` when a pool is
      feasible for its SLA (and never for `interactive`).
- [ ] 2.3 Verify the interactive path end to end on the deployed broker: an `asr`
      admit with `sla=interactive` is granted when a node has room, is queued when
      none does, and is NEVER placed on a cold pool. Evidence recorded in this change.

## 3. Qualify the classifier rung

- [ ] 3.1 `scripts/incident-corpus.mts` against the broker's ledger. Verify: the
      report names the classes it is too thin to speak for, rather than a bare total.
- [ ] 3.2 Confirm labels with a person for at least the holdout, so no case is
      `agent_only`. Verify: jingway's evaluator no longer HOLDS the holdout.
- [ ] 3.3 `scripts/observe-incidents.mts` over the balanced schedule against the live
      classifier, then jingway's `scripts/evaluate-decisions.ts`. Verify: the report
      carries accepted-decision correctness, dangerous-action errors counted
      separately from misses, abstention and coverage, invariant rejections, order
      disagreement over distinct orderings, and full-cascade cost and p95 including
      the fallback.
- [ ] 3.4 Publish the receipt under `fleetd/receipts/`, and update
      `_plans/fleetd-weave-jev.md` to SHIPPED (shadow) with the numbers.

## 4. Verification before archive

- [ ] 4.1 Evidence for 1.2, 1.3, 1.4 and 2.3 recorded in this change.
- [ ] 4.2 `openspec validate --specs` green.
