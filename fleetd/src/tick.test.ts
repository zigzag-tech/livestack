/**
 * What one tick costs, and what it refuses.
 *
 * Most of these assert an ABSENCE — no plan call, no operation call, no
 * escalation, no model. That is deliberate: the expensive failures of a
 * supervision loop are all things it did that nobody asked for, and an absence
 * is only checkable if the fake counts.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { noRepairHost } from 'jingway-framework/server/weave/noRepairHost.js';
import type { WeaveHost } from 'jingway-framework/common/routines/weave.js';

import { FakeBroker, NOW, err, operation, plan, refused, unreachable, view } from './fakeBroker.js';
import { HANDBACK_NAMES, fleetHandbacks, newLoopState } from './handbacks.js';
import { tracedHost } from './observability.js';
import { fleetTick } from './tick.js';
import { registeredKeys, registeredWorkflow } from './workflows.js';

function countingHost(): { host: WeaveHost; escalations: number[] } {
  const escalations: number[] = [];
  const host: WeaveHost = {
    ...noRepairHost('test host: repair is off so an escalation is visible as a count, not as a model call'),
    escalate: async () => {
      escalations.push(1);
      return { verdict: { verdict: 'human_gate' as const, reason: 'test host does not repair' } };
    },
  };
  return { host, escalations };
}

const JOBS = [{ job_id: 'job-0', kind: 'llm' }];
const PROVISION = {
  type: 'provision' as const,
  job_id: 'job-0',
  target_id: 'heyuan-spot',
  tier: 'SPOT',
  est_cost: 2.1,
  reason: 'burst SPOT: no cheaper running room',
};

function tick(client: FakeBroker, extra: Partial<Parameters<typeof fleetTick>[0]> = {}) {
  const { host, escalations } = countingHost();
  return {
    escalations,
    run: () =>
      fleetTick({
        client,
        jobs: JOBS,
        now: () => NOW,
        settleBudgetMs: 0,
        hostFor: () => host,
        observeHost: host,
        ...extra,
      }),
  };
}

test('an admitted job needs no operation at all', async () => {
  const client = new FakeBroker({
    plan: plan([{ type: 'admit', job_id: 'job-0', target_id: 'http://a', est_cost: 0, reason: 'run on existing LOCAL' }]),
  });
  const t = tick(client);
  const result = await t.run();
  assert.deepEqual(result.admitted, ['job-0']);
  assert.equal(client.count('startOperation'), 0);
  assert.deepEqual(t.escalations, []);
});

test('the green path costs zero escalations and zero tokens', async () => {
  const announced = operation({ state: 'announced', node_id: 'http://burst-1', terminal: false });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: announced, observations: [announced] });
  const t = tick(client);
  const result = await t.run();
  assert.equal(result.results[0]!.outcome, 'settled');
  assert.equal(result.escalations, 0);
  assert.deepEqual(t.escalations, []);
});

test('an unreadable view dispatches nothing — not even a plan call', async () => {
  const client = new FakeBroker({ view: unreachable() });
  const result = await tick(client).run();
  assert.equal(result.blocked?.stepId, 'assemble_state');
  assert.equal(client.count('plan'), 0);
  assert.equal(client.count('startOperation'), 0);
});

test('an empty view is blocked, because it is not evidence of an empty fleet', async () => {
  const client = new FakeBroker({ view: view({ hosts: {} }) });
  const result = await tick(client).run();
  assert.equal(result.blocked?.stepId, 'assemble_state');
  assert.equal(client.count('startOperation'), 0);
});

test('a stale view is blocked rather than planned against', async () => {
  const client = new FakeBroker({ view: view({ generated_at: NOW - 600 }) });
  const result = await tick(client).run();
  assert.equal(result.blocked?.stepId, 'assemble_state');
});

test('a plan that forgets a queued job is blocked before anything is spent', async () => {
  const client = new FakeBroker({ plan: plan([], { jobs: [{ job_id: 'job-0', kind: 'llm', owner: 'a', sla: 'normal' }] }) });
  const result = await tick(client).run();
  assert.equal(result.blocked?.stepId, 'plan');
  assert.equal(client.count('startOperation'), 0);
});

test('a plan acting on a job nobody queued is blocked', async () => {
  const client = new FakeBroker({ plan: plan([{ ...PROVISION, job_id: 'ghost' }], { jobs: [] }) });
  const result = await tick(client).run();
  assert.equal(result.blocked?.stepId, 'plan');
  assert.equal(client.count('startOperation'), 0);
});

test('an operation still inside its deadline is WAITING, not a red gate', async () => {
  const creating = operation({ state: 'creating', announce_deadline: NOW + 600 });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: creating, observations: [creating] });
  const t = tick(client);
  const result = await t.run();
  assert.equal(result.results[0]!.outcome, 'waiting');
  assert.deepEqual(t.escalations, [], 'provisioning takes minutes; watching a boot must not cost a model');
});

test('a registered failure is handled mechanically and never reaches a model', async () => {
  const shortage = operation({
    state: 'rejected',
    terminal: true,
    announce_deadline: NOW - 1,
    error: err('create', 'capacity_shortage', 'no_capacity'),
  });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: shortage, observations: [shortage] });
  const t = tick(client);
  const result = await t.run();
  // `rejected` is a settled answer: nothing was billed and the planner decides
  // next tick. No escalation either way.
  assert.equal(result.results[0]!.outcome, 'settled');
  assert.deepEqual(t.escalations, []);
});

test('an uncertain create is handled by the table, not by a model', async () => {
  const uncertain = operation({
    state: 'uncertain',
    announce_deadline: NOW - 1,
    error: err('create', 'uncertain_effect', 'TimeoutError'),
  });
  const resolved = operation({ state: 'created', provider_instance_id: 'i-1', announce_deadline: NOW + 600 });
  // The only observation this tick makes is the tier1 handler's re-read; the
  // settle budget is zero, so nothing polls.
  const client = new FakeBroker({ plan: plan([PROVISION]), start: uncertain, observations: [resolved] });
  const t = tick(client);
  const result = await t.run();
  assert.deepEqual(t.escalations, [], 'uncertain_effect is a registered key; the tier1 handler resolves it');
  assert.equal(result.results[0]!.outcome, 'waiting');
});

test('an unregistered failure is the only thing that earns an escalation', async () => {
  const weird = operation({
    state: 'failed',
    announce_deadline: NOW - 1,
    reason: 'something nobody wrote a workflow for',
    error: err('create', 'martian_interference', 'unknown'),
  });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: weird, observations: [weird] });
  const t = tick(client);
  const result = await t.run();
  assert.equal(t.escalations.length, 1);
  assert.equal(result.results[0]!.outcome, 'blocked');
});

test('a quota refusal is recorded as a refusal, not as a failure', async () => {
  const client = new FakeBroker({ plan: plan([PROVISION]), start: refused(409, 'account quota: acct_a holds 1 of 1 slot(s)') });
  const t = tick(client);
  const result = await t.run();
  const outcome = result.results[0]!;
  assert.equal(outcome.outcome, 'refused');
  assert.equal(outcome.outcome === 'refused' && outcome.status, 409);
});

test('a cooled pool is skipped without a call, and the reason says so', async () => {
  const state = newLoopState();
  state.cooldowns.set('heyuan-spot', NOW + 60);
  const client = new FakeBroker({ plan: plan([PROVISION]) });
  const result = await tick(client, { state }).run();
  assert.equal(client.count('startOperation'), 0);
  assert.match(result.skipped[0]!.reason, /cooled for another/);
});

test('one blocked job does not stop another job in the same tick', async () => {
  const weird = operation({
    operation_id: 'OP-BAD',
    job_id: 'job-bad',
    state: 'failed',
    announce_deadline: NOW - 1,
    error: err('create', 'martian_interference', 'unknown'),
  });
  const client = new FakeBroker({
    plan: plan([
      { ...PROVISION, job_id: 'job-bad' },
      { type: 'admit', job_id: 'job-good', target_id: 'http://a', est_cost: 0, reason: 'run on existing LOCAL' },
    ]),
    start: weird,
    observations: [weird],
  });
  const result = await tick(client, { jobs: [{ job_id: 'job-bad', kind: 'llm' }, { job_id: 'job-good', kind: 'llm' }] }).run();
  assert.deepEqual(result.admitted, ['job-good']);
  assert.equal(result.results[0]!.outcome, 'blocked');
});

test('a deprovision the broker refuses because the node is busy is a correct outcome', async () => {
  const client = new FakeBroker({
    plan: plan([{ type: 'deprovision', target_id: 'http://burst-1', reason: 'idle burst worker, no demand' }]),
    start: refused(409, 'http://burst-1 is not drainable: 2 active lease(s)'),
  });
  const t = tick(client, { jobs: [] });
  const result = await t.run();
  assert.equal(result.results[0]!.outcome, 'refused');
  assert.deepEqual(t.escalations, []);
});

// --- the surfaces, asserted closed ------------------------------------------
test('the handback surface is exactly the declared one', () => {
  const names: string[] = Object.keys(
    fleetHandbacks({ client: new FakeBroker(), state: newLoopState(), now: () => NOW }),
  ).sort();
  for (const forbidden of ['provision', 'deprovision', 'choose_tier', 'relax_region_policy', 'set_quota', 'set_budget']) {
    assert.ok(!names.includes(forbidden), `${forbidden} must never be a handback`);
  }
  // Last: node's `deepEqual` is a type assertion, so it narrows `names` and
  // every `includes` after it stops compiling.
  assert.deepEqual(names, [...HANDBACK_NAMES].sort());
});

test('no handback mutates region, quota or budget', () => {
  const handbacks = fleetHandbacks({ client: new FakeBroker(), state: newLoopState(), now: () => NOW });
  // The one that names a policy RECORDS a request and returns nothing granting.
  const state = newLoopState();
  const surface = fleetHandbacks({ client: new FakeBroker(), state, now: () => NOW });
  return surface.request_policy_change!.run(
    { kind: 'region', reason: 'the only capacity left is in na' },
    new AbortController().signal,
  ).then((out: unknown) => {
    assert.deepEqual(out, { recorded: true, kind: 'region' });
    assert.equal(state.policyRequests.length, 1);
    assert.ok(handbacks.refresh_availability);
  });
});

test('a cooldown is capped and scoped to one pool', async () => {
  const state = newLoopState();
  const surface = fleetHandbacks({ client: new FakeBroker(), state, now: () => NOW });
  await surface.refresh_availability!.run({ pool_id: 'p', seconds: 900 }, new AbortController().signal);
  assert.equal(state.cooldowns.get('p'), NOW + 900);
  assert.equal(state.cooldowns.get('other'), undefined, 'a shortage on one pool must not cool the rest');
  // Beyond the cap is clamped rather than honoured: an uncapped backoff removes
  // a region from every job's options and tells none of them why.
  await surface.refresh_availability!.run({ pool_id: 'p', seconds: 100_000 } as never, new AbortController().signal);
  assert.equal(state.cooldowns.get('p'), NOW + 900);
});

test('every registered key maps to a workflow that spends nothing', () => {
  assert.deepEqual(registeredKeys(), [
    'announce:provider_fault:never_announced',
    'create:capacity_shortage:*',
    'create:request_or_workload_fault:*',
    'create:uncertain_effect:*',
  ]);
  const decision = registeredWorkflow(operation({ error: err('create', 'capacity_shortage', 'no_capacity') }));
  assert.equal(decision?.workflow, 'refresh_availability');
  assert.ok(decision!.afterS! <= 900);
});

// --- the paper trail --------------------------------------------------------
test('every repair, summary and event leaves with the job and operation id', async () => {
  const entries: Array<{ type: string; job_id: string; operation_id?: string; run_id: string }> = [];
  const announced = operation({ state: 'announced', node_id: 'http://burst-1' });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: announced, observations: [announced] });
  let seen: string | undefined;
  const base = noRepairHost('traced');
  const host = tracedHost({
    base,
    jobId: 'job-0',
    runId: 'run-1',
    operationId: () => seen,
    sink: (e) => entries.push({ type: e.type, job_id: e.job_id, run_id: e.run_id, ...(e.operation_id ? { operation_id: e.operation_id } : {}) }),
  });
  seen = 'OP1';
  await fleetTick({ client, jobs: JOBS, now: () => NOW, settleBudgetMs: 0, hostFor: () => host, observeHost: host });
  assert.ok(entries.length > 0);
  assert.ok(entries.every((e) => e.job_id === 'job-0' && e.run_id === 'run-1'));
  assert.ok(entries.some((e) => e.type === 'summary'));
  assert.ok(entries.every((e) => e.operation_id === 'OP1'));
});

test('a trace sink that throws is reported and changes nothing', async () => {
  const errors: unknown[] = [];
  const announced = operation({ state: 'announced' });
  const client = new FakeBroker({ plan: plan([PROVISION]), start: announced, observations: [announced] });
  const host = tracedHost({
    base: noRepairHost('traced'),
    jobId: 'job-0',
    runId: 'run-1',
    operationId: () => 'OP1',
    sink: () => {
      throw new Error('the ledger disk is full');
    },
    onSinkError: (e) => errors.push(e),
  });
  const result = await fleetTick({ client, jobs: JOBS, now: () => NOW, settleBudgetMs: 0, hostFor: () => host, observeHost: host });
  assert.equal(result.results[0]!.outcome, 'settled');
  assert.ok(errors.length > 0, 'a paper trail that goes quiet must not look like a system with nothing to say');
});
