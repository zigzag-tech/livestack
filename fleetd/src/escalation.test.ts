/**
 * The ladder, in shadow: the classifier records and the deterministic path
 * decides.
 *
 * The assertions are about authority, not accuracy. In shadow the classifier is
 * allowed to be wrong and must not be allowed to matter — and when it is
 * unreachable it must reach a person rather than a provider.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { EscalationPacket, WeaveHost } from 'jingway-framework/common/routines/weave.js';

import type { ClassifyResult, IncidentDecisionRecord } from './classify.js';
import { classifyingHost } from './escalation.js';
import { NOW, err, operation } from './fakeBroker.js';

const PACKET = {
  routineId: 'fleet_tick',
  stepId: 'operation:provision:job-0',
  blockGoal: 'g',
  goal: 'g',
  code: '',
  attempts: [],
  evidence: {},
  handbacks: [],
  budgetRemaining: { attempts: 0, escalations: 1 },
  effect: { class: 'irreversible' as const },
  performed: [],
} satisfies EscalationPacket;

const LIMITS = { wallMs: 1000, maxSteps: 2, signal: new AbortController().signal };

function baseHost(verdict: 'resume' | 'carry_through' | 'human_gate' = 'human_gate') {
  const calls: number[] = [];
  const host: WeaveHost = {
    conversationId: 'c1',
    escalate: async () => {
      calls.push(1);
      return { verdict: { verdict, reason: 'the deterministic path decided' } };
    },
    record: async () => {},
    emit: () => {},
  };
  return { host, calls };
}

function result(over: Partial<ClassifyResult['outcome']> & { status: ClassifyResult['outcome']['status'] }): ClassifyResult {
  const record = {
    decision_id: 'd1',
    operation_id: 'OP1',
    job_id: 'job-0',
    task_id: 'fleet.failure_class',
    task_version: 'v',
    profile_id: 'p',
    mode: 'shadow' as const,
    evidence_revision: 'r',
    evidence_digest: 'dig',
    submitted_order: ['capacity_shortage'],
    acceptance_policy_version: 'a',
    order_policy_version: 'o',
    outcome: 'answered' as const,
    invariant_violations: [],
    applied: false,
    missing_metadata: [],
    at: NOW,
  } satisfies IncidentDecisionRecord;
  return { record, raw: {} as never, outcome: over as ClassifyResult['outcome'] };
}

test('a shadow selection is recorded and the deterministic path still decides', async () => {
  const { host, calls } = baseHost();
  const records: IncidentDecisionRecord[] = [];
  const composed = classifyingHost({
    base: host,
    operationFor: () => operation({ state: 'failed', error: err('create', 'martian', 'x') }),
    classify: async () =>
      result({ status: 'shadow', failureClass: 'capacity_shortage', workflow: 'refresh_availability', applied: false } as never),
    persist: (r) => {
      records.push(r);
    },
  });
  const outcome = await composed.escalate(PACKET, [], LIMITS);
  assert.equal(calls.length, 1, 'the repair turn still ran; the classifier decided nothing');
  assert.equal(records.length, 1);
  assert.equal(records[0]!.applied, false);
  assert.match(outcome.notes ?? '', /\[shadow\] failure_class=capacity_shortage/);
  assert.match(outcome.notes ?? '', /nothing was done with it/);
});

test('an unreachable classifier blocks for a person and never reaches the repair turn', async () => {
  const { host, calls } = baseHost('resume');
  const composed = classifyingHost({
    base: host,
    operationFor: () => operation({ state: 'failed' }),
    classify: async () =>
      result({
        status: 'unavailable',
        humanBlock: true,
        reason: 'unavailable. The classifier shares the fleet\'s own LLM capacity, so nothing is provisioned to restore it.',
      } as never),
    persist: () => {},
  });
  const outcome = await composed.escalate(PACKET, [], LIMITS);
  assert.equal(outcome.verdict.verdict, 'human_gate');
  assert.match(outcome.verdict.reason ?? '', /nothing is provisioned to restore it/);
  assert.equal(calls.length, 0);
});

test('a classifier that THREW is an outage, not a quiet fall-through', async () => {
  const { host, calls } = baseHost('resume');
  const errors: unknown[] = [];
  const composed = classifyingHost({
    base: host,
    operationFor: () => operation({ state: 'failed' }),
    classify: async () => {
      throw new Error('ECONNREFUSED');
    },
    persist: () => {},
    onError: (e) => errors.push(e),
  });
  const outcome = await composed.escalate(PACKET, [], LIMITS);
  assert.equal(outcome.verdict.verdict, 'human_gate');
  assert.equal(calls.length, 0);
  assert.equal(errors.length, 1);
});

test('an incident with no operation goes straight to the repair turn', async () => {
  const { host, calls } = baseHost();
  let classified = 0;
  const composed = classifyingHost({
    base: host,
    operationFor: () => undefined,
    classify: async () => {
      classified += 1;
      return undefined;
    },
    persist: () => {},
  });
  await composed.escalate(PACKET, [], LIMITS);
  assert.equal(classified, 0, 'nothing to classify means no model call at all');
  assert.equal(calls.length, 1);
});

test('an invariant rejection is recorded and says no runner-up was taken', async () => {
  const { host } = baseHost();
  const composed = classifyingHost({
    base: host,
    operationFor: () => operation({ state: 'uncertain' }),
    classify: async () => result({ status: 'rejected', violations: ['the effect is unresolved'] } as never),
    persist: () => {},
  });
  const outcome = await composed.escalate(PACKET, [], LIMITS);
  assert.match(outcome.notes ?? '', /no runner-up was taken/);
});

test('a persist that throws is reported and changes nothing', async () => {
  const { host, calls } = baseHost();
  const errors: unknown[] = [];
  const composed = classifyingHost({
    base: host,
    operationFor: () => operation({ state: 'failed' }),
    classify: async () =>
      result({ status: 'shadow', failureClass: 'provider_fault', workflow: 'schedule_wakeup', applied: false } as never),
    persist: () => {
      throw new Error('the trace store is full');
    },
    onError: (e) => errors.push(e),
  });
  const outcome = await composed.escalate(PACKET, [], LIMITS);
  assert.equal(calls.length, 1);
  assert.equal(errors.length, 1);
  assert.equal(outcome.verdict.verdict, 'human_gate');
});
