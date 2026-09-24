/**
 * A person's approve / revert (task 5.3), against a fake broker holding real policy files,
 * an improver database the real improver filled, and the real livestack-policy binary.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ACTIVATION_OPS } from 'jingway-framework/common/routines/activation.js';

import { approveProposal, main, revertPolicy } from './approve.js';
import { POLICY_ID } from './source.js';
import { activeFromLedger, readProposal, upsertShadowWindow, type LedgerOp } from './store.js';
import { FIXTURE_THRESHOLDS, NEEDS_BIN, world, type World } from './testkit.js';

/** A world where the improver has bootstrapped and made one proposal. */
async function proposed(): Promise<{ w: World; id: string; version: string }> {
  const w = await world();
  const s = await w.run(FIXTURE_THRESHOLDS);
  assert.equal(s.outcome, 'proposed', s.failed.join('\n'));
  return { w, id: s.proposal!.id, version: s.proposal!.version };
}

async function shadowEvidence(w: World, version: string, days: number, floorLo = 0) {
  for (let d = 0; d < days; d++) {
    const from = new Date(w.now.getTime() - (d + 1) * 86_400_000);
    await upsertShadowWindow(w.store, {
      policyId: POLICY_ID,
      candidateVersion: version,
      from,
      to: new Date(from.getTime() + 86_400_000),
      decisions: 500,
      agreementRate: 0.7,
      floors: ['lease_expired', 'caller_ok'].map((outcome) => ({ outcome, ci: { lo: floorLo, hi: 0.01 }, ess: 400 })),
    });
  }
}

const last = (ops: LedgerOp[]) => ops[ops.length - 1];
const approvals = (ops: LedgerOp[]) => ops.filter((o) => o.opType === 'human_patch_approval');

test('--shadow records who approved, then shadows it; routing is unchanged', { skip: NEEDS_BIN }, async () => {
  const { w, id, version } = await proposed();
  try {
    const result = await approveProposal({ store: w.store, broker: w.broker, proposalId: id, mode: 'shadow', approver: 'alice' });
    assert.equal(result.ok, true, result.message);
    assert.deepEqual(w.broker.calls.filter((c) => c.startsWith('put')), ['put:shadow']);
    assert.deepEqual(w.broker.shadow.map((a) => a.version), [version]);
    assert.equal(w.broker.active?.version, w.meta.active.version, 'the active artifact did not move');
    const ops = await w.store.ops(POLICY_ID);
    assert.equal(last(ops).opType, 'human_patch_approval');
    assert.deepEqual(
      { approver: last(ops).payload.approver, role: last(ops).payload.role, proposalId: last(ops).payload.proposalId },
      { approver: 'alice', role: 'shadow', proposalId: id },
    );
    assert.equal((await readProposal(w.store, id))?.status, 'shadow');
  } finally {
    await w.close();
  }
});

test('the approval is recorded before the publish, so a failed PUT still names who asked', { skip: NEEDS_BIN }, async () => {
  const { w, id } = await proposed();
  try {
    w.broker.failPut = new Error('broker said 503');
    await assert.rejects(
      approveProposal({ store: w.store, broker: w.broker, proposalId: id, mode: 'shadow', approver: 'alice' }),
      /503/,
    );
    assert.equal(approvals(await w.store.ops(POLICY_ID)).length, 1);
    assert.equal((await readProposal(w.store, id))?.status, 'proposed', 'not marked shadowed when the PUT failed');
  } finally {
    await w.close();
  }
});

test('--activate is refused until the proposal has passed the shadow rung', { skip: NEEDS_BIN }, async () => {
  const { w, id, version } = await proposed();
  try {
    const noWindows = await approveProposal({
      store: w.store, broker: w.broker, cli: w.cli, proposalId: id, mode: 'activate', approver: 'alice',
    });
    assert.equal(noWindows.ok, false);
    assert.match(noWindows.message, /has not passed the shadow rung — shadow failed \(no_evidence\): 0 shadow window/);
    assert.deepEqual(noWindows.rungs?.map((r) => `${r.rung}:${r.passed}`),
      ['deterministic:true', 'cornerstones:true', 'regression_corpus:true', 'shadow:false']);

    // Enough windows, but a guardrail regressed beyond its bound in them.
    await shadowEvidence(w, version, 3, -0.5);
    const regressed = await approveProposal({
      store: w.store, broker: w.broker, cli: w.cli, proposalId: id, mode: 'activate', approver: 'alice',
    });
    assert.equal(regressed.ok, false);
    assert.match(regressed.message, /floor_regressed/);

    assert.deepEqual(w.broker.calls.filter((c) => c.startsWith('put')), [], 'nothing was published');
    assert.deepEqual(approvals(await w.store.ops(POLICY_ID)), [], 'a refusal records no approval');
    assert.equal(activeFromLedger(await w.store.ops(POLICY_ID))?.version, w.meta.active.version);
  } finally {
    await w.close();
  }
});

test('--activate after the shadow rung publishes, records, and the next run sees no drift; revert goes back', { skip: NEEDS_BIN }, async () => {
  const { w, id, version } = await proposed();
  try {
    await shadowEvidence(w, version, 3);
    const result = await approveProposal({
      store: w.store, broker: w.broker, cli: w.cli, proposalId: id, mode: 'activate', approver: 'alice',
    });
    assert.equal(result.ok, true, result.message);
    assert.deepEqual(w.broker.calls.filter((c) => c.startsWith('put')), ['put:active']);
    assert.equal(w.broker.active?.version, version);
    let ops = await w.store.ops(POLICY_ID);
    const approval = approvals(ops)[0];
    assert.deepEqual({ role: approval.payload.role, rung: approval.payload.approvedAtRung }, { role: 'active', rung: 'shadow' });
    assert.equal(last(ops).opType, ACTIVATION_OPS.activated);
    assert.ok(approval.seq < last(ops).seq, 'approval recorded before the activation');
    assert.equal(last(ops).payload.activatedBy, 'human:alice');
    assert.equal(activeFromLedger(ops)?.version, version);
    assert.equal((await readProposal(w.store, id))?.status, 'activated');

    const next = await w.run();
    assert.notEqual(next.outcome, 'drift', next.failed.join('\n'));
    assert.equal(next.ledgerActive, version);

    // Revert: the ledger's previous is the broker's previous, so the file swap lands on it.
    const reverted = await revertPolicy({ store: w.store, broker: w.broker, policyId: POLICY_ID, approver: 'bob' });
    assert.equal(reverted.ok, true, reverted.message);
    assert.equal(w.broker.calls.filter((c) => c === 'revert').length, 1);
    assert.equal(w.broker.active?.version, w.meta.active.version);
    ops = await w.store.ops(POLICY_ID);
    assert.equal(activeFromLedger(ops)?.version, w.meta.active.version);
    assert.equal(last(ops).payload.sourceRef, `revert:${version}`);
    assert.equal(last(ops).payload.activatedBy, 'human:bob');
    assert.notEqual((await w.run()).outcome, 'drift');
  } finally {
    await w.close();
  }
});

test('revert refuses when the broker would swap in something the ledger did not name', { skip: NEEDS_BIN }, async () => {
  const { w, id, version } = await proposed();
  try {
    const only = await revertPolicy({ store: w.store, broker: w.broker, policyId: POLICY_ID, approver: 'bob' });
    assert.equal(only.ok, false);
    assert.match(only.message, /only one active artifact/);

    await shadowEvidence(w, version, 3);
    assert.equal((await approveProposal({
      store: w.store, broker: w.broker, cli: w.cli, proposalId: id, mode: 'activate', approver: 'alice',
    })).ok, true);
    w.broker.previous = { version: 'b3:0123' };
    const wrong = await revertPolicy({ store: w.store, broker: w.broker, policyId: POLICY_ID, approver: 'bob' });
    assert.equal(wrong.ok, false);
    assert.match(wrong.message, /a file-swap revert would activate something the ledger did not name/);
    assert.equal(w.broker.calls.filter((c) => c === 'revert').length, 0);
  } finally {
    await w.close();
  }
});

test('approval on top of drift is refused', { skip: NEEDS_BIN }, async () => {
  const { w, id } = await proposed();
  try {
    w.broker.reportActive = 'b3:00ff';
    const result = await approveProposal({ store: w.store, broker: w.broker, proposalId: id, mode: 'shadow', approver: 'alice' });
    assert.equal(result.ok, false);
    assert.match(result.message, /policy_projection_drift/);
    assert.deepEqual(w.broker.calls.filter((c) => c.startsWith('put')), []);
    assert.deepEqual(approvals(await w.store.ops(POLICY_ID)), []);
  } finally {
    await w.close();
  }
});

test('the CLI names the operator or refuses', async () => {
  assert.equal(await main([], {}), 64);
  assert.equal(await main(['tp-x', '--shadow', '--activate'], { USER: 'alice' }), 64);
  // No $USER: refused before any database or broker is touched.
  assert.equal(await main(['tp-x', '--shadow'], {}), 2);
});
