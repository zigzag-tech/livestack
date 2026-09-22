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

- [ ] 2.1 **Decide who owns the queue**, and record the decision with its reasons.
      Three shapes, and they are not equivalent: (a) `hostd` retains jobs it answered
      with `Queue` and `fleetd` reads them — the broker gains state it has so far
      refused to hold; (b) `POST /fleet/plan` grows an "include the broker's queued
      jobs" mode — same state, narrower surface; (c) the caller keeps its own queue and
      re-presents it — no new broker state, and every caller has to implement it.
      Until one is chosen, `Queue` is a dead end and no burst can ever be triggered by
      a real request.
- [ ] 2.2 Implement it. Verify: a job the broker answers with `Queue` appears in a
      subsequent `POST /fleet/plan` and, when a pool is feasible for its SLA, produces
      a `provision`. Tests: a queued job reaches a plan; a job that was admitted does
      not; a queued job whose SLA cannot tolerate the provision latency stays queued.
      Ledger: the `admit` record and the later `operation` records join on `job_id`.
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
