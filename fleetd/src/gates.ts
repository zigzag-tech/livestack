/**
 * The deterministic floor. Cheap, no model, no network beyond the observation
 * the step already made.
 *
 * Every gate here reads a CORRELATED FACT and refuses to read anything else.
 * That is the whole discipline: the tempting gate for a provision is "did a
 * fresh node appear?", which is almost always true on a live fleet and
 * therefore proves nothing about the create we just paid for. The gate that
 * means something is "does the broker say THIS operation reached a state that
 * settles it?" — and the broker only says that when a node carrying this
 * operation's id reported ready.
 */
import type { Violation } from 'jingway-framework/common/routines/contract.js';
import type { GateResult } from 'jingway-framework/common/routines/weave.js';
import type { FleetPlan, FleetView, Operation } from './client.js';

/** How stale the view a plan was computed from may be. */
export const DEFAULT_VIEW_MAX_AGE_S = 60;

export function viewIsFresh(
  view: FleetView | undefined,
  now: number,
  maxAgeS = DEFAULT_VIEW_MAX_AGE_S,
): Violation[] {
  if (!view) return [{ rule: 'view', msg: 'the fleet view could not be read at all' }];
  const age = now - (view.generated_at ?? 0);
  if (!Number.isFinite(age) || age < 0) {
    return [
      {
        rule: 'view-clock',
        msg: `the view is stamped ${view.generated_at}, which is ahead of this loop's clock — one of the two is wrong and neither may be assumed`,
      },
    ];
  }
  if (age > maxAgeS) {
    return [{ rule: 'view-stale', msg: `the fleet view is ${age.toFixed(0)}s old (limit ${maxAgeS}s)` }];
  }
  const hosts = Object.values(view.hosts ?? {});
  if (hosts.length === 0) {
    // Not a pass. An empty fleet and an unreadable one produce the same JSON,
    // and a loop that reads the second as the first plans against nothing and
    // reports a healthy tick.
    return [{ rule: 'view-empty', msg: 'the view lists no host at all; that is not evidence the fleet is empty' }];
  }
  return [];
}

export function planIsWellFormed(plan: FleetPlan | undefined): Violation[] {
  if (!plan) return [{ rule: 'plan', msg: 'no plan was returned' }];
  const out: Violation[] = [];
  if (!plan.plan_version) out.push({ rule: 'plan-version', msg: 'the plan carries no plan_version, so nothing may be dispatched against it' });
  if (!Array.isArray(plan.actions)) {
    out.push({ rule: 'plan-actions', msg: 'the plan has no actions array' });
    return out;
  }
  const jobIds = new Set(plan.jobs?.map((j) => j.job_id) ?? []);
  for (const action of plan.actions) {
    if ('job_id' in action && action.job_id && !jobIds.has(action.job_id)) {
      out.push({
        rule: 'plan-unknown-job',
        msg: `the plan acts on ${action.job_id}, which was not in the queue it was handed`,
        failing: [action],
      });
    }
    if (!('reason' in action) || !action.reason) {
      out.push({ rule: 'plan-unreasoned', msg: `action ${action.type} carries no reason`, failing: [action] });
    }
  }
  // Every queued job must have exactly one action. A job the plan simply forgot
  // is the failure nobody notices: it looks like a quiet fleet.
  const decided = new Set(plan.actions.flatMap((a) => ('job_id' in a && a.job_id ? [a.job_id] : [])));
  for (const id of jobIds) {
    if (!decided.has(id)) out.push({ rule: 'plan-silent', msg: `job ${id} got no action at all` });
  }
  return out;
}

/**
 * The gate for one operation step.
 *
 * Green on `announced` — the correlated receipt — and green on a TERMINAL
 * refusal, because "the provider has no capacity and said so" is a settled
 * answer this step cannot improve on; the planner's next tick decides what to
 * do about it. Red on anything still in flight, which is what an escalation is
 * for, and red on a state the operation should have left.
 */
export function operationSettled(op: Operation | undefined, now: number): GateResult {
  const base = { decidedBy: 'invariant' as const, observationFingerprint: op?.operation_id };
  if (!op) return { ...base, violations: [{ rule: 'operation-missing', msg: 'the broker returned no operation for this id' }] };
  switch (op.state) {
    case 'announced':
    case 'released':
      return { ...base, violations: [] };
    case 'rejected':
      // Settled and cheap: nothing was billed, and the reason is in the record.
      return { ...base, violations: [] };
    case 'failed':
      return {
        ...base,
        violations: [
          {
            rule: 'operation-failed',
            msg:
              `${op.operation_id} failed: ${op.reason ?? 'no reason recorded'}` +
              (op.provider_instance_id ? ` (instance ${op.provider_instance_id} may still be billing)` : ''),
            failing: [op.error],
          },
        ],
      };
    case 'uncertain':
      return {
        ...base,
        violations: [
          {
            rule: 'operation-uncertain',
            msg: `${op.operation_id}: the create's effect is unknown and must be reconciled, never retried`,
            failing: [op.error],
          },
        ],
      };
    default: {
      const waited = now - (op.created_at ?? now);
      return {
        ...base,
        violations: [
          {
            rule: 'operation-in-flight',
            msg: `${op.operation_id} is still ${op.state} after ${waited.toFixed(0)}s`,
            failing: [op.error],
          },
        ],
      };
    }
  }
}

/**
 * The gate the operation STEP actually uses.
 *
 * Green on a settled operation AND on one that is still progressing inside its
 * deadline, because that is precisely the block's stated goal: *every queued job
 * holds a claimed target, or a bounded operation is progressing toward one.* A
 * gate that went red on "still creating" would open a repair turn every tick for
 * a machine that is booting exactly as expected — and paying a model to watch a
 * boot is the failure mode this design exists to avoid.
 *
 * Red is reserved for the three things that are genuinely wrong: it failed, its
 * effect is unknown, or it ran out of deadline while still in flight.
 */
export function operationOnTrack(op: Operation | undefined, now: number): GateResult {
  if (op && isWaiting(op, now)) {
    return {
      decidedBy: 'invariant',
      observationFingerprint: op.operation_id,
      violations: [],
    };
  }
  return operationSettled(op, now);
}

/**
 * Is this operation still moving, or does it need a person?
 *
 * Used to decide whether a red gate is a WAIT (schedule a wakeup, spend
 * nothing) or an incident. Provisioning takes minutes; treating every
 * mid-flight observation as a failure would open a repair turn every tick for
 * a machine that is booting exactly as expected.
 */
export function isWaiting(op: Operation, now: number): boolean {
  if (op.state !== 'creating' && op.state !== 'created' && op.state !== 'intent') return false;
  const deadline = op.announce_deadline ?? Infinity;
  return now < deadline;
}
