/**
 * One improver run (task 5.2), against the fixture stream, a fake broker and the real
 * livestack-policy binary.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { ACTIVATION_OPS } from 'jingway-framework/common/routines/activation.js';

import { OBJECTIVE_STATUS } from './improver.js';
import { POLICY_ID } from './source.js';
import { activeFromLedger, shadowWindows, type LedgerOp } from './store.js';
import { FIXTURE_THRESHOLDS, NEEDS_BIN, world } from './testkit.js';

const opTypes = (ops: LedgerOp[]) => ops.map((o) => o.opType);
const publishes = (calls: string[]) => calls.filter((c) => c.startsWith('put') || c === 'revert');

test('bootstrap activates what the broker already decides with, as hand-authored, and publishes nothing', { skip: NEEDS_BIN }, async () => {
  const w = await world();
  try {
    const first = await w.run();
    assert.equal(first.bootstrapped, true);
    assert.equal(first.ledgerActive, w.meta.active.version);
    const ops = await w.store.ops(POLICY_ID);
    assert.deepEqual(opTypes(ops), [ACTIVATION_OPS.activated]);
    assert.equal(ops[0].payload.source, 'code');
    const payload = ops[0].payload.policy as unknown as { objective: { outcomeId: string; status: string }; guardrails: unknown[] };
    assert.equal(payload.objective.outcomeId, 'job_wall_s');
    assert.equal(payload.objective.status, OBJECTIVE_STATUS);
    assert.equal(payload.guardrails.length, 2);
    assert.deepEqual(publishes(w.broker.calls), [], 'a bootstrap must not re-PUT (it would overwrite .previous.json)');

    const second = await w.run();
    assert.equal(second.bootstrapped, false);
    assert.deepEqual(opTypes(await w.store.ops(POLICY_ID)), [ACTIVATION_OPS.activated]);
  } finally {
    await w.close();
  }
});

test('a broker on compiled defaults cannot be bootstrapped from, and the run says so', async () => {
  const w = await world({ seedBroker: false });
  try {
    const s = await w.run();
    assert.equal(s.outcome, 'blocked');
    assert.match(s.failed.join('\n'), /policy_artifact_missing/);
    assert.deepEqual(await w.store.ops(POLICY_ID), []);
  } finally {
    await w.close();
  }
});

test('drift: a broker deciding with another version stops the run before any tuning', { skip: NEEDS_BIN }, async () => {
  const w = await world();
  try {
    await w.run();
    w.broker.reportActive = 'b3:00ff';
    const s = await w.run();
    assert.equal(s.outcome, 'drift');
    assert.match(s.failed.join('\n'), /policy_projection_drift: the activation ledger says b3:[0-9a-f]+ is active; the broker decides with b3:00ff/);
    assert.equal(s.tune, null, 'nothing was tuned');
    assert.deepEqual(publishes(w.broker.calls), []);
  } finally {
    await w.close();
  }
});

test('a run that proposes nothing names why', { skip: NEEDS_BIN }, async () => {
  const w = await world();
  try {
    // Production thresholds: 600 fixture decisions are fewer than the 2 000 required.
    const s = await w.run({});
    assert.equal(s.outcome, 'no_proposal');
    assert.equal(s.tune?.selfCheck, 'passed', 'the replay reproduced every logged decision');
    assert.match(s.failed.join('\n'), /no proposal: preconditions not met: \d+ joined non-self-traffic decisions, needs 2000/);
    assert.equal(s.proposal, null);
  } finally {
    await w.close();
  }
});

test('a parameter change that truly improves the objective is proposed, and reconcile still sees no drift', { skip: NEEDS_BIN }, async () => {
  const w = await world();
  try {
    const s = await w.run(FIXTURE_THRESHOLDS);
    assert.equal(s.outcome, 'proposed', s.failed.join('\n'));
    const proposal = s.proposal!;
    const { active } = w.meta;

    // The fixture's truth: `spot` is ~40 s faster, and today's params tie toward `local`.
    // Every admissible improvement breaks that tie toward `spot`.
    const row = await w.store.db.selectFrom('policy_proposals').selectAll().where('id', '=', proposal.id).executeTakeFirstOrThrow();
    const artifact = row.artifact as { params: Record<string, number>; version: string; parent_version: string };
    assert.equal(artifact.parent_version, active.version);
    const p = artifact.params;
    const a = active.params as Record<string, number>;
    assert.ok(
      p.w_resource * p.local_bonus < a.w_resource * a.local_bonus || p.w_budget > a.w_budget,
      `the proposal must favour spot: ${JSON.stringify(p)}`,
    );
    const evidence = row.evidence as { evidence: { evaluation: { objective: { lift: { value: number }; correctedCi: { lo: number } } } } };
    assert.ok(evidence.evidence.evaluation.objective.lift.value > 30, 'about 40 s of wall time saved per job');
    assert.ok(evidence.evidence.evaluation.objective.correctedCi.lo > 0);

    const doc = await readFile(proposal.doc, 'utf8');
    assert.match(doc, new RegExp(`npm run policy-approve -- ${proposal.id} --shadow`));
    assert.match(doc, /job_wall_s/);
    assert.match(doc, /`regression_corpus`: \*\*passed\*\*/);
    assert.deepEqual(publishes(w.broker.calls), [], 'a proposal changes no routing');

    // The activation TABLE's single row now names the proposal...
    const row2 = await w.store.activation({ actor: 'test' }).get(POLICY_ID);
    assert.equal(row2?.status, 'proposed');
    assert.equal(row2?.version, proposal.version);
    // ...but the ledger still knows what is active, so the next run finds no drift.
    const ops = await w.store.ops(POLICY_ID);
    assert.deepEqual(opTypes(ops), [ACTIVATION_OPS.activated, ACTIVATION_OPS.proposed]);
    assert.equal(activeFromLedger(ops)?.version, active.version);
    const again = await w.run(FIXTURE_THRESHOLDS);
    assert.notEqual(again.outcome, 'drift', again.failed.join('\n'));
    assert.equal(again.ledgerActive, active.version);
    if (again.proposal) assert.equal(again.proposal.alreadyProposed, true, 'the same artifact is not proposed twice');
  } finally {
    await w.close();
  }
});

test('shadow windows are evaluated for what the broker shadows', { skip: NEEDS_BIN }, async () => {
  const w = await world();
  try {
    w.broker.shadow = [w.meta.shadow];
    const s = await w.run();
    assert.equal(s.shadow.length, 1);
    assert.ok(s.shadow[0].windows >= 3, `whole days in the window: ${s.shadow[0].windows}`);
    const rows = await shadowWindows(w.store, POLICY_ID, w.meta.shadow.version);
    assert.equal(rows.length, s.shadow[0].windows);
    // Whole UTC days; the first and last hold only part of the fixture's 4.2 days.
    assert.equal(rows.reduce((n, r) => n + r.decisions, 0), w.meta.decisions);
    for (const r of rows) {
      // The shadow (w_budget 1.5) always picks spot; the active artifact's greedy is local.
      assert.equal(r.agreementRate, 0);
      assert.ok(r.decisions > 0 && r.decisions <= 144);
      assert.equal(r.to.getTime() - r.from.getTime(), 86_400_000);
      assert.deepEqual(r.floors.map((f) => f.outcome), ['lease_expired', 'caller_ok']);
      // Flat guardrails in the fixture: no expiry, every caller ok — no regression either way.
      for (const f of r.floors) assert.deepEqual(f.ci, { lo: 0, hi: 0 });
    }
  } finally {
    await w.close();
  }
});
