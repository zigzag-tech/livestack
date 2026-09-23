/**
 * The closed effect surface. Everything a repair turn may ask code to do, and
 * nothing else.
 *
 * What is NOT here is the design: no `provision`, no `choose_tier`, no
 * `relax_region_policy`, no `raise_quota`, no `set_budget`. A repair turn can
 * observe the fleet, resolve an unresolved create, cool a pool, wait, and ASK a
 * person for a policy change. It cannot grant itself one.
 *
 * That constraint is not caution about models specifically. A one-token answer
 * that could select LAST_RESORT would bypass the lexicographic guard in
 * `fleet_scheduler.schedule()` that makes "last resort" literal, and the guard
 * is the thing that stops a bad minute from becoming an expensive hour. Any
 * surface that can bypass it is the same defect whether a model, a retry loop
 * or a tired operator drives it.
 */
import { z } from 'zod';
import type { Handback } from 'jingway-framework/common/routines/weave.js';
import { READ_EFFECT } from 'jingway-framework/common/routines/effects.js';
import type { FleetClient, Operation } from './client.js';
import { COOLDOWN_S, MAX_COOLDOWN_S } from './workflows.js';

/** Where a cooled pool and a pending wakeup live for the length of a tick. */
export interface LoopState {
  /** pool id -> unix seconds until which it is cooled. */
  cooldowns: Map<string, number>;
  /** job id -> unix seconds at which to look again. */
  wakeups: Map<string, number>;
  /** Policy changes a repair turn asked a person for. Requests, never grants. */
  policyRequests: Array<{ kind: 'region' | 'quota' | 'budget'; reason: string; at: number }>;
}

export function newLoopState(): LoopState {
  return { cooldowns: new Map(), wakeups: new Map(), policyRequests: [] };
}

const OperationShape = z.object({
  operation_id: z.string(),
  state: z.string(),
  reason: z.string().nullable().optional(),
  provider_instance_id: z.string().nullable().optional(),
  node_id: z.string().nullable().optional(),
});

export interface HandbackDeps {
  client: FleetClient;
  state: LoopState;
  now: () => number;
}

export function fleetHandbacks(deps: HandbackDeps): Record<string, Handback<any, any>> {
  const { client, state, now } = deps;
  return {
    read_fleet_view: {
      description: 'The whole fleet as the broker sees it: every node, its state, how long unseen, and the operations outstanding.',
      input: z.object({}),
      output: z.object({ generated_at: z.number(), hosts: z.record(z.string(), z.unknown()) }).passthrough(),
      effect: READ_EFFECT,
      run: async () => client.view(),
    },

    read_operation: {
      description: 'One provisioning operation: its state, its receipts and its structured error.',
      input: z.object({ operation_id: z.string() }),
      output: OperationShape.passthrough(),
      effect: READ_EFFECT,
      run: async ({ operation_id }) => client.operation(operation_id),
    },

    read_ledger_since: {
      description: 'Decision records this broker wrote since a unix timestamp — what it decided and why each loser lost.',
      input: z.object({ since: z.number(), limit: z.number().int().min(1).max(500).optional() }),
      output: z.object({ records: z.array(z.unknown()) }).passthrough(),
      effect: READ_EFFECT,
      run: async ({ since, limit }) => client.ledgerSince(since, limit ?? 200),
    },

    reconcile_operation: {
      description:
        'Resolve an operation whose create outcome is unknown by ASKING the provider. Bills nothing and never creates: it can only turn `uncertain` into the truth.',
      input: z.object({ operation_id: z.string() }),
      output: OperationShape.passthrough(),
      // A read of the provider that WRITES the resolution to the operation
      // store. Ledger class, because the store deduplicates by operation id —
      // re-running it is safe because of a mechanism, not an assertion.
      effect: { class: 'ledger', idempotencyKey: ({ stepId, attempt }) => `reconcile:${stepId}:${attempt}` },
      run: async ({ operation_id }, signal) => {
        if (signal.aborted) throw new Error('block_expired');
        // Reconciliation is the broker's own operation: asking it to observe
        // the operation is what triggers it. The loop never talks to a cloud API.
        return client.operation(operation_id) as Promise<Operation>;
      },
    },

    refresh_availability: {
      description:
        'Mark a pool as out of capacity for a bounded, scoped cooldown, then re-plan. Does not choose another pool and does not change any policy.',
      input: z.object({
        pool_id: z.string(),
        seconds: z.number().int().min(15).max(MAX_COOLDOWN_S).optional(),
      }),
      output: z.object({ pool_id: z.string(), cooled_until: z.number() }),
      effect: { class: 'session' },
      run: async ({ pool_id, seconds }, signal) => {
        if (signal.aborted) throw new Error('block_expired');
        // Capped and scoped. An uncapped backoff on a shared pool removes a
        // region from every job's options, and none of them are told why.
        const until = now() + Math.min(seconds ?? COOLDOWN_S, MAX_COOLDOWN_S);
        state.cooldowns.set(pool_id, until);
        return { pool_id, cooled_until: until };
      },
    },

    schedule_wakeup: {
      description: 'Look at this job again in N seconds. Spends nothing; it is how waiting is expressed.',
      input: z.object({ job_id: z.string(), after_s: z.number().int().min(1).max(3600) }),
      output: z.object({ job_id: z.string(), at: z.number() }),
      effect: { class: 'session' },
      run: async ({ job_id, after_s }, signal) => {
        if (signal.aborted) throw new Error('block_expired');
        const at = now() + after_s;
        state.wakeups.set(job_id, at);
        return { job_id, at };
      },
    },

    request_policy_change: {
      description:
        'Ask a PERSON to change a region policy, a quota or a budget. It records a request. It grants nothing, and the job it was raised for stays under the policy in force.',
      input: z.object({
        kind: z.enum(['region', 'quota', 'budget']),
        reason: z.string().min(1).max(2000),
      }),
      output: z.object({ recorded: z.literal(true), kind: z.string() }),
      effect: { class: 'ledger', idempotencyKey: ({ stepId, attempt }) => `policy-request:${stepId}:${attempt}` },
      run: async ({ kind, reason }, signal) => {
        if (signal.aborted) throw new Error('block_expired');
        state.policyRequests.push({ kind, reason, at: now() });
        return { recorded: true as const, kind };
      },
    },
  };
}

/**
 * The names this surface offers, in order. Asserted in a test: the surface is
 * closed, and something that grows a `provision` handback should fail loudly
 * rather than quietly become able to spend.
 */
export const HANDBACK_NAMES = [
  'read_fleet_view',
  'read_operation',
  'read_ledger_since',
  'reconcile_operation',
  'refresh_availability',
  'schedule_wakeup',
  'request_policy_change',
] as const;
