/**
 * `fleet_tick` — one pass of the supervision loop.
 *
 * The loop SUPERVISES; it does not decide. Every placement comes from
 * `POST /fleet/plan` and every effect goes through `POST /fleet/operations`. It
 * holds no capacity state of its own, which is what makes a restart of this
 * process cost nothing: the plan is a read, and the claim is already on the
 * broker's disk.
 *
 * ## Shape, and one departure from the design sketch
 *
 * `_plans/fleetd-weave-jev.md` §3 draws one weave per tick with the operation
 * steps inside it. That is one conversation, and
 * `openspec/.../fleet-supervision-loop/spec.md` requires escalations to open in
 * a conversation bound **per job** — a blocked operation must not stop an
 * unrelated job from being admitted. Both are satisfied by splitting the tick
 * where the ownership changes:
 *
 * 1. one READ-ONLY block (`assemble_state` → `plan`) over the whole fleet, and
 * 2. one block per acting job, each with its own host and its own conversation.
 *
 * Per-job blocks run concurrently, so a job sitting at a human gate delays
 * nothing. The gates and the effect surface are identical either way; only the
 * conversation boundary moved, and it moved to where the spec put it.
 *
 * ## What the green path costs
 *
 * Nothing. Gates are deterministic; a plan whose operations reach `announced`
 * inside their deadline escalates zero times and spends zero tokens. A red gate
 * consults the registered workflow table BEFORE any model sees it, and only an
 * incident nobody registered gets to be interesting.
 */
import { weave } from 'jingway-framework/server/weave/weave.js';
import { noRepairHost } from 'jingway-framework/server/weave/noRepairHost.js';
import { READ_EFFECT } from 'jingway-framework/common/routines/effects.js';
import type { WeaveHost, WeaveOutcome } from 'jingway-framework/common/routines/weave.js';
import type { RepairRecord } from 'jingway-framework/common/routines/contract.js';

import {
  BrokerUnreachable,
  OperationRefused,
  idempotencyKey,
  type FleetClient,
  type FleetPlan,
  type ActingAction,
  type FleetView,
  type JobRequest,
  type Operation,
  type PlanAction,
} from './client.js';
import { DEFAULT_VIEW_MAX_AGE_S, isWaiting, operationOnTrack, planIsWellFormed, viewIsFresh } from './gates.js';
import { fleetHandbacks, newLoopState, type LoopState } from './handbacks.js';
import { registeredWorkflow, triage, type WorkflowDecision } from './workflows.js';

export const ROUTINE_ID = 'fleet_tick';
export const ROUTINE_GOAL =
  'Every queued job holds a claimed target, or a bounded operation is progressing toward one, under the effective policy.';
export const ROUTINE_POLICY = [
  'Never grant yourself a policy change (region, quota, budget). You may only request one.',
  'A create whose result is unknown is reconciled, never retried. There is no handback that creates.',
  'Choose human_gate when the state contradicts itself, or when the only remaining action would spend money.',
].join(' ');

export interface TickDeps {
  client: FleetClient;
  /** The queue this tick is about. The loop does not own it; a caller does. */
  jobs: JobRequest[];
  /** A conversation per job. Default: no repair at all, stated as such. */
  hostFor?: (jobId: string) => WeaveHost;
  /** Where the read-only half runs. It has no effects, so it never escalates. */
  observeHost?: WeaveHost;
  state?: LoopState;
  now?: () => number;
  regions?: string;
  viewMaxAgeS?: number;
  /**
   * How long one operation step waits for its operation to settle before the
   * gate looks. Bounded: a step that waited forever would hold the tick, and a
   * step that did not wait at all would escalate on every boot.
   */
  settleBudgetMs?: number;
  pollEveryMs?: number;
  log?: (message: string) => void;
}

export type ActionOutcome =
  | { action: ActingAction; outcome: 'settled'; operation: Operation }
  | { action: ActingAction; outcome: 'waiting'; operation: Operation; afterS: number }
  | { action: ActingAction; outcome: 'refused'; status: number; detail: string }
  | { action: ActingAction; outcome: 'blocked'; reason: string; operation?: Operation };

export interface TickResult {
  view?: FleetView;
  plan?: FleetPlan;
  /** Jobs the plan admitted to an existing worker. Nothing to supervise. */
  admitted: string[];
  queued: Array<{ job_id: string; reason: string }>;
  results: ActionOutcome[];
  /** Actions this tick deliberately did not take, and why. */
  skipped: Array<{ action: ActingAction; reason: string }>;
  repairs: RepairRecord[];
  escalations: number;
  /** Set when the read-only half could not produce a usable plan. */
  blocked?: { stepId: string; reason: string };
  state: LoopState;
}

export async function fleetTick(deps: TickDeps): Promise<TickResult> {
  const now = deps.now ?? (() => Date.now() / 1000);
  const state = deps.state ?? newLoopState();
  const log = deps.log ?? (() => {});
  const viewMaxAgeS = deps.viewMaxAgeS ?? DEFAULT_VIEW_MAX_AGE_S;
  const result: TickResult = {
    admitted: [],
    queued: [],
    results: [],
    skipped: [],
    repairs: [],
    escalations: 0,
    state,
  };

  const observe = await weave<{ view: FleetView; plan: FleetPlan }>(
    deps.observeHost ??
      noRepairHost(
        'the read-only half of a fleet tick has no effects to repair; a broker that will not answer is an outage, not an incident',
      ),
    { id: `${ROUTINE_ID}:observe`, goal: ROUTINE_GOAL, handbacks: {}, budget: { wallMs: 60_000 } },
    async (step) => {
      const view = await step({
        id: 'assemble_state',
        goal: 'Read the fleet as the broker sees it, recently enough to plan against.',
        effect: READ_EFFECT,
        run: () => deps.client.view(),
        gate: (v) => viewIsFresh(v, now(), viewMaxAgeS),
      });
      const plan = await step({
        id: 'plan',
        goal: 'Get one action for every queued job from the broker, which is the only thing that decides placement.',
        effect: READ_EFFECT,
        run: () => deps.client.plan({ jobs: deps.jobs, ...(deps.regions ? { regions: deps.regions } : {}) }),
        gate: (p) => planIsWellFormed(p),
      });
      return { view, plan };
    },
  );
  collect(result, observe);
  if (!observe.ok) {
    result.blocked = { stepId: observe.blocked.stepId, reason: observe.blocked.reason };
    log(`[fleetd] tick blocked at ${observe.blocked.stepId}: ${observe.blocked.reason}`);
    return result;
  }
  result.view = observe.value.view;
  result.plan = observe.value.plan;

  const acting: ActingAction[] = [];
  for (const action of observe.value.plan.actions) {
    if (action.type === 'admit') {
      result.admitted.push(action.job_id);
      continue;
    }
    if (action.type === 'queue') {
      result.queued.push({ job_id: action.job_id, reason: action.reason });
      continue;
    }
    const skip = skipReason(action, state, now());
    if (skip) {
      result.skipped.push({ action, reason: skip });
      continue;
    }
    acting.push(action);
  }

  // Concurrently, and each in its own conversation: a job at a human gate must
  // not hold up a job that is fine. This is the whole reason the per-job split
  // exists — see the module docstring.
  const outcomes = await Promise.all(
    acting.map((action) =>
      runAction(action, observe.value.plan, { ...deps, now, state, log }).then((r) => {
        collect(result, r.outcome);
        return r.result;
      }),
    ),
  );
  result.results.push(...outcomes);
  return result;
}

function skipReason(action: ActingAction, state: LoopState, at: number): string | undefined {
  if (action.type === 'provision') {
    const cooled = state.cooldowns.get(action.target_id);
    if (cooled && cooled > at) {
      return `${action.target_id} is cooled for another ${Math.round(cooled - at)}s after a capacity shortage`;
    }
    const wake = state.wakeups.get(action.job_id);
    if (wake && wake > at) {
      return `${action.job_id} is waiting until ${new Date(wake * 1000).toISOString()}`;
    }
  }
  return undefined;
}

interface RunDeps extends TickDeps {
  now: () => number;
  state: LoopState;
  log: (m: string) => void;
}

async function runAction(
  action: ActingAction,
  plan: FleetPlan,
  deps: RunDeps,
): Promise<{ result: ActionOutcome; outcome: WeaveOutcome<ActionOutcome> }> {
  const jobId = 'job_id' in action && action.job_id ? action.job_id : action.target_id;
  const host =
    deps.hostFor?.(jobId) ??
    noRepairHost(
      `no repair host is bound for ${jobId}; the operation is recorded on the broker and a person has to look`,
      { conversationId: `fleet-job:${jobId}` },
    );
  const settleBudgetMs = deps.settleBudgetMs ?? 30_000;
  const pollEveryMs = deps.pollEveryMs ?? 2_000;
  let current: Operation | undefined;

  const outcome = await weave<ActionOutcome>(
    host,
    {
      id: ROUTINE_ID,
      goal: ROUTINE_GOAL,
      policy: ROUTINE_POLICY,
      handbacks: fleetHandbacks({ client: deps.client, state: deps.state, now: deps.now }),
      budget: { wallMs: 120_000, escalations: 1 },
    },
    async (step) =>
      step<ActionOutcome>({
        id: `operation:${action.type}:${jobId}`,
        goal:
          action.type === 'provision'
            ? `Hold a claimed target for ${jobId}, or have a bounded operation progressing toward one.`
            : `Release ${action.target_id} only if it is empty; a refusal because it is busy is a correct outcome.`,
        // It creates or destroys a billable machine. There is no undo, so the
        // driver must never re-run it — `reobserve` is how a repair sees the
        // world afterwards.
        effect: {
          class: 'irreversible',
          note:
            action.type === 'provision'
              ? 'creates a billable instance; the claim is already durable on the broker before this runs'
              : 'releases a worker; running jobs on it would be killed',
        },
        // Two attempts, and the second one can only be an observe-only tier1
        // variant: the driver refuses to re-run an irreversible step's `run`.
        // One attempt would mean every red gate escalates, including the ones
        // the workflow table already answers for free.
        budget: { attempts: 2, escalations: 1 },
        run: async () => {
          const started = await start(action, plan, deps);
          if (started.outcome !== 'settled' && started.outcome !== 'waiting') return started;
          current = started.operation;
          current = await settle(current, deps, settleBudgetMs, pollEveryMs);
          return describe(action, current, deps.now());
        },
        reobserve: async () => {
          if (!current) return { action, outcome: 'blocked', reason: 'no operation was ever started' };
          current = await deps.client.operation(current.operation_id);
          return describe(action, current, deps.now());
        },
        evidence: () => ({
          action,
          plan_version: plan.plan_version,
          operation: current,
          uncertainty: plan.uncertainty,
          excluded: plan.excluded,
        }),
        // Mechanical first, model second. A failure the table already knows is
        // handled with zero tokens; only an unregistered one earns a turn.
        tier1: [
          {
            when: () => Boolean(current && registeredWorkflow(current)),
            then: async () => {
              const decision = triage(current!, deps.now(), isWaiting);
              deps.log(`[fleetd] ${current!.operation_id}: ${decision.workflow} — ${decision.reason}`);
              applyWorkflow(decision, current!, deps);
              // A gate_only step's tier1 must not repeat the effect. This
              // re-observes; the workflow it ran creates nothing.
              current = await deps.client.operation(current!.operation_id);
              return describe(action, current, deps.now());
            },
          },
        ],
        gate: (value) => {
          if (value && value.outcome === 'refused') {
            // A deprovision the broker refused because the node is busy is the
            // control plane working, not failing.
            return { decidedBy: 'invariant' as const, violations: [] };
          }
          return operationOnTrack(current, deps.now());
        },
      }),
  );

  if (outcome.ok) return { result: outcome.value, outcome };
  return {
    result: {
      action,
      outcome: 'blocked',
      reason: outcome.blocked.reason,
      ...(current ? { operation: current } : {}),
    },
    outcome,
  };
}

async function start(action: ActingAction, plan: FleetPlan, deps: RunDeps): Promise<ActionOutcome> {
  try {
    const operation = await deps.client.startOperation({
      action,
      planVersion: plan.plan_version,
      idempotencyKey: idempotencyKey(action),
      ...(deps.regions ? { regions: deps.regions } : {}),
    });
    return { action, outcome: 'settled', operation };
  } catch (error) {
    if (error instanceof OperationRefused) {
      // A stated refusal. Recorded as itself: "409, this owner is at its
      // ceiling" wants a different response from "the broker is down", and a
      // loop that cannot tell them apart retries one of them forever.
      deps.log(`[fleetd] ${action.type} refused (${error.status}): ${error.detail}`);
      return { action, outcome: 'refused', status: error.status, detail: error.detail };
    }
    if (error instanceof BrokerUnreachable) throw error;
    throw error;
  }
}

async function settle(
  operation: Operation,
  deps: RunDeps,
  budgetMs: number,
  everyMs: number,
): Promise<Operation> {
  const until = Date.now() + budgetMs;
  let current = operation;
  while (!current.terminal && current.state !== 'announced' && Date.now() < until) {
    await sleep(everyMs);
    current = await deps.client.operation(current.operation_id);
  }
  return current;
}

function describe(action: ActingAction, operation: Operation, at: number): ActionOutcome {
  if (isWaiting(operation, at)) {
    const afterS = Math.max(15, Math.min(60, Math.round((operation.announce_deadline ?? at) - at)));
    return { action, outcome: 'waiting', operation, afterS };
  }
  return { action, outcome: 'settled', operation };
}

function applyWorkflow(decision: WorkflowDecision | { workflow: 'wait'; reason: string; afterS: number }, op: Operation, deps: RunDeps) {
  switch (decision.workflow) {
    case 'refresh_availability':
      deps.state.cooldowns.set(op.target_id, deps.now() + (decision.afterS ?? 120));
      return;
    case 'wait':
    case 'schedule_wakeup':
      deps.state.wakeups.set(op.job_id, deps.now() + (decision.afterS ?? 60));
      return;
    case 'reconcile_operation':
    case 'investigate':
    case 'request_policy_change':
      // Recorded, not acted on here: reconcile is the broker's own move (a read
      // of the operation triggers it), and the other two are for a person.
      return;
  }
}

function collect(result: TickResult, outcome: WeaveOutcome<unknown>) {
  result.repairs.push(...outcome.repairs);
  result.escalations += outcome.repairs.length;
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
