/**
 * The corpus, and the two ways a measurement lies about itself: a split that
 * lets one incident appear on both sides, and a residual class stuffed with
 * everything nobody could label.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { balancedSchedule, freezeDecisionCases, scheduleBalance } from 'jingway-framework/server/decisions';

import { FAILURE_CLASSES } from './classify.js';
import { casesFromLedger, describeCorpus, ledgerLabel, operationsFromLedger, toCases } from './corpus.js';

const CANDIDATES = Object.keys(FAILURE_CLASSES);

test('an incident nobody can label is dropped, not filed under needs_investigation', () => {
  assert.equal(ledgerLabel({ stage: 'create', class: 'martian_interference', code: 'x' }), undefined);
  assert.equal(ledgerLabel(null), undefined);
  assert.equal(ledgerLabel({ class: 'capacity_shortage' }), 'capacity_shortage');
  const cases = toCases([{ operationId: 'OP1', error: { class: 'martian_interference' } }]);
  assert.deepEqual(cases, [], 'stuffing the residual class teaches a measurement that it is the common one');
});

test('every observation of one operation shares a split', () => {
  const cases = toCases([
    { operationId: 'OP1', state: 'uncertain', error: { class: 'uncertain_effect' } },
    { operationId: 'OP1', state: 'failed', error: { class: 'uncertain_effect' } },
    { operationId: 'OP2', state: 'rejected', error: { class: 'capacity_shortage' } },
  ]);
  assert.equal(new Set(cases.map((c) => c.sourceKey)).size, 2);
  const frozen = freezeDecisionCases(cases);
  const where = (id: string) =>
    (['development', 'calibration', 'holdout'] as const).filter((split) =>
      frozen[split].some((c) => c.sourceKey === id),
    );
  assert.equal(where('OP1').length, 1, 'one incident must not straddle two splits');
});

test('a ledger record becomes a case only when it carries a class we offer', () => {
  const cases = casesFromLedger([
    { decision: 'operation', request: { operation_id: 'OP1' }, outcome: { state: 'rejected', error: { class: 'capacity_shortage', stage: 'create', code: 'no_capacity' } } },
    { decision: 'operation', request: { operation_id: 'OP2' }, outcome: { state: 'announced' } },
    { decision: 'claim', request: { operation_id: 'OP3' }, outcome: { state: 'rejected', error: { class: 'capacity_shortage' } } },
  ]);
  assert.deepEqual(cases.map((c) => c.sourceKey), ['OP1']);
});

test('a label nobody confirmed is marked as such', () => {
  const cases = casesFromLedger([
    { decision: 'operation', request: { operation_id: 'OP1' }, outcome: { error: { class: 'provider_fault' } } },
  ]);
  assert.equal(cases[0]!.labelSource, 'agent_only');
  assert.equal(describeCorpus(cases).agentOnly, 1);
});

test('the corpus names the classes it is too thin to speak for', () => {
  const cases = toCases(
    Array.from({ length: 12 }, (_, i) => ({ operationId: `OP${i}`, error: { class: 'capacity_shortage' } })),
  );
  const report = describeCorpus(cases, 10);
  assert.equal(report.total, 12);
  assert.equal(report.byClass.capacity_shortage, 12);
  assert.deepEqual(report.thin.sort(), CANDIDATES.filter((c) => c !== 'capacity_shortage').sort());
});

test('the permutation schedule is balanced and every ordering distinct is named', () => {
  const schedule = balancedSchedule(CANDIDATES, 'fleet-incident-seed-1');
  assert.equal(schedule.length, CANDIDATES.length * 2);
  assert.ok(scheduleBalance(schedule).balanced);
  // Same candidates, different order: cost, never sample size.
  const distinct = new Set(schedule.map((p) => p.order.join('>')));
  assert.equal(distinct.size, schedule.length, 'five distinct labels give ten distinct orderings');
});

test('a replayed operation leaves absent fields absent', () => {
  const [op] = Object.values(
    operationsFromLedger([
      {
        decision: 'operation',
        ts: 1700000000,
        request: { operation_id: 'OP1', job_id: 'job-0', owner: 'acct_a' },
        outcome: { state: 'failed', error: { stage: 'create', class: 'provider_fault', code: '503' } },
      },
    ]),
  );
  assert.equal(op!.provider_instance_id, null, 'a reconstruction must not invent a receipt');
  assert.equal(op!.node_id, null);
  assert.equal(op!.state, 'failed');
});
