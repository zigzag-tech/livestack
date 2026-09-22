/**
 * The registered workflow table: what code does about a failure it already
 * understands.
 *
 * This table is the reason the loop is cheap. A red gate consults it FIRST, and
 * a structured error whose `{stage, class, code}` appears here never reaches a
 * model — no escalation, no tokens, no conversation. Only an incident nobody
 * registered gets to be interesting.
 *
 * Two rules keep it honest:
 *
 * 1. **A workflow may not spend.** Nothing here provisions, changes a region,
 *    raises a quota or picks a tier. The most a workflow does is reconcile
 *    (which bills nothing by construction), cool a pool down, wait, or open a
 *    request for a person. Placement stays with `schedule()`.
 * 2. **Waiting is a state, not a failure.** An operation inside its deadline
 *    resolves to `wait` with a wakeup, not to a red gate that escalates. A loop
 *    that escalated every mid-flight observation would open a repair turn every
 *    tick for a machine that is booting exactly as expected.
 */
import type { Operation, StructuredError } from './client.js';

export type WorkflowName =
  | 'reconcile_operation'
  | 'refresh_availability'
  | 'schedule_wakeup'
  | 'investigate'
  | 'request_policy_change';

export interface WorkflowDecision {
  workflow: WorkflowName;
  /** Why, in a sentence that ends up in the ledger and in a person's inbox. */
  reason: string;
  /** For `schedule_wakeup` / `refresh_availability`: how long, in seconds. */
  afterS?: number;
  /** For `request_policy_change`: which policy a person is being asked about. */
  policy?: 'region' | 'quota' | 'budget';
}

/** `stage:class:code`, or `stage:class:*` for a whole class. */
export type ErrorKey = string;

export function keyOf(error: Pick<StructuredError, 'stage' | 'class' | 'code'>): ErrorKey {
  return `${error.stage}:${error.class}:${error.code}`;
}

/**
 * How long a pool stays cooled after a capacity shortage.
 *
 * Capped and scoped deliberately. An uncapped backoff on a shared pool is how
 * one job's bad minute removes a whole region from the fleet's options for an
 * hour, and every other job pays for it without ever being told why.
 */
export const COOLDOWN_S = 120;
export const MAX_COOLDOWN_S = 900;

const TABLE: Record<ErrorKey, (op: Operation) => WorkflowDecision> = {
  'create:capacity_shortage:*': (op) => ({
    workflow: 'refresh_availability',
    afterS: COOLDOWN_S,
    reason: `${op.provider ?? 'the provider'} has no capacity for ${op.target_id}; cool it for ${COOLDOWN_S}s and re-plan`,
  }),
  'create:uncertain_effect:*': (op) => ({
    workflow: 'reconcile_operation',
    reason: `${op.operation_id}: the create's effect is unknown; ask the provider, never retry`,
  }),
  'create:request_or_workload_fault:*': (op) => ({
    workflow: 'investigate',
    reason: `${op.operation_id}: the provider refused the request (${op.error?.code ?? 'no code'}); a person has to look at the spec, image, credential or quota`,
  }),
  'announce:provider_fault:never_announced': (op) => ({
    workflow: 'investigate',
    reason: `${op.operation_id}: the instance was created and never became usable; instance ${op.provider_instance_id ?? 'unknown'} may still be billing`,
  }),
};

/**
 * The workflow for this operation, or `undefined` when nobody registered one —
 * which is exactly the condition that earns an escalation.
 */
export function registeredWorkflow(op: Operation): WorkflowDecision | undefined {
  if (!op.error) return undefined;
  const exact = TABLE[keyOf(op.error)];
  if (exact) return exact(op);
  const byClass = TABLE[`${op.error.stage}:${op.error.class}:*`];
  return byClass ? byClass(op) : undefined;
}

/** Every key the table answers — asserted in tests so the surface stays closed. */
export function registeredKeys(): ErrorKey[] {
  return Object.keys(TABLE).sort();
}

/**
 * What to do about an operation whose gate went red. `wait` is a first-class
 * answer and the most common one.
 */
export function triage(
  op: Operation,
  now: number,
  waiting: (op: Operation, now: number) => boolean,
): WorkflowDecision | { workflow: 'wait'; reason: string; afterS: number } {
  if (waiting(op, now)) {
    const remaining = Math.max(15, Math.round((op.announce_deadline ?? now) - now));
    return {
      workflow: 'wait',
      afterS: Math.min(remaining, 60),
      reason: `${op.operation_id} is ${op.state} and inside its deadline; provisioning takes minutes`,
    };
  }
  return (
    registeredWorkflow(op) ?? {
      workflow: 'investigate',
      reason: `${op.operation_id}: ${op.error ? `${keyOf(op.error)} is not a registered failure` : 'no structured error was recorded'}`,
    }
  );
}
