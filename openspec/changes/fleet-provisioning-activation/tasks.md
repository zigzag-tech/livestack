## 1. Activate the lifecycle (operator)

- [x] 1.0 **Ship the lifecycle to the fleet broker WITHOUT pools.** Done 2026-09-22
      02:36 CST on `xc-tower-ubuntu:8801`. livestack `6de47145` cut to
      `~/.local/share/livestack-releases/fleet-provisioning-6de47145/`, pinned by
      `livestack-fleetd.service.d/70-fleet-provisioning.conf`. A release directory
      rather than a pull, per the convention in `_plans/fleet-caller-identity.md` R.4 —
      rollback is `rm` of that one drop-in plus a restart.
      Evidence: `GET /fleet/operations` → `200 {"operations":[]}`; `GET /fleet/ledger`
      → `200` with records; `GET /fleet` now carries `pools: []`, `demand` (256 shapes,
      120 s TTL) and `operations` (store at
      `~/.cache/livestack/fleet-operations.sqlite3`, bound 5000, age window disabled).
      Membership converged to 18 peers / 15 hosts / 18 fresh (pre-restart baseline:
      18 / 15 / 17). Zero tracebacks since restart. **The observe-only property still
      holds and is proven by absence: zero `[hostbroker] evict|warm` lines in this
      journal.** The host broker on `:8799` is unaffected (`200`).
      **`pools: []`, so the broker still cannot provision** — that is 1.1, and it is
      the only thing standing between here and a real operation.

- [x] 1.0b **Credential installed and proven, 2026-09-22.** Located on zz-tower2 at
      `/etc/default/unchain-gateway` (the unchain gateway's key — the ECS/ECI/ACR/OSS
      one). Only the two `ALIBABA_CLOUD_ACCESS_KEY_*` lines were copied host-to-host
      into `/etc/livestack/fleet-provider.env` (`0600 root:root`, 116 bytes); the value
      was never displayed. Proven with `scripts/check_provider_credentials.py`:
      *OK: aliyun answered DescribeInstances in cn-heyuan.* Read-only, nothing created.
      **Two findings this turned up, both worth acting on separately:**
      (a) the source file is `mode=664 owner=ubuntu:ubuntu` — the Alibaba key is
      readable by any user on zz-tower2. Not caused by this change and not fixed by it;
      it should be `0600`.
      (b) this key carries OSS and ACR access as well as ECS. The recommendation to
      mint a RAM user scoped to `RunInstances`/`DescribeInstances`/`DeleteInstances` in
      one region still stands — it narrows a leak from "read the content store and push
      to the registry" to "rented machines in cn-heyuan".
- [ ] 1.1 **Credential done (1.0b); the POOL declaration is what remains.** A search of the
      Declare `LIVESTACK_FLEET_POOLS` on the fleet broker with one pool, a real
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
