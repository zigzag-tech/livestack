/**
 * A hand-authored artifact revision published through the ledger (7.1/7.2), against a fake
 * broker holding real policy files, an improver database the real improver bootstrapped,
 * and the real livestack-policy binary (which computes every version here).
 */
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';

import { ACTIVATION_OPS } from 'jingway-framework/common/routines/activation.js';
import type { PolicyArtifact } from 'jingway-framework/server/policy';

import { OBJECTIVE_STATUS } from './improver.js';
import { CONFIRMED, main, publishArtifact } from './publish.js';
import { POLICY_ID } from './source.js';
import { activeFromLedger, type LedgerOp } from './store.js';
import { NEEDS_BIN, world, type World } from './testkit.js';

/** A world whose ledger the improver has bootstrapped from the broker's artifact. */
async function bootstrapped(): Promise<World> {
  const w = await world();
  const s = await w.run();
  assert.equal(s.bootstrapped, true, s.failed.join('\n'));
  return w;
}

/** v0 with exploration on, parented as given, sealed (versioned) by the binary. */
async function revision(w: World, overrides: Partial<PolicyArtifact> = {}): Promise<{ file: string; artifact: PolicyArtifact }> {
  const file = path.join(w.root, `rev-${Math.random().toString(36).slice(2)}.json`);
  const artifact = await w.cli.seal({
    ...w.meta.active,
    parent_version: w.meta.active.version,
    exploration: { enabled: true, epsilon: 0.05, margin: 0.25 },
    provenance: { created_by: 'human:alice', created_at: '2026-09-24T00:00:00Z', notes: 'exploration on' },
    ...overrides,
  }, file);
  return { file, artifact };
}

const last = (ops: LedgerOp[]) => ops[ops.length - 1];
const puts = (w: World) => w.broker.calls.filter((c) => c.startsWith('put'));
const publish = (w: World, file: string, confirmObjective = false) =>
  publishArtifact({ store: w.store, broker: w.broker, cli: w.cli, artifactPath: file, operator: 'alice', confirmObjective });

test('publishes, records a hand-authored activation, and the next improver run sees no drift', { skip: NEEDS_BIN }, async () => {
  const w = await bootstrapped();
  try {
    const { file, artifact } = await revision(w);
    const result = await publish(w, file);
    assert.equal(result.ok, true, result.message);
    assert.deepEqual(puts(w), ['put:active']);
    assert.equal(w.broker.active?.version, artifact.version);
    assert.equal(w.broker.previous?.version, w.meta.active.version);

    const ops = await w.store.ops(POLICY_ID);
    const approval = ops.filter((o) => o.opType === 'human_patch_approval').at(-1)!;
    assert.equal(last(ops).opType, ACTIVATION_OPS.activated);
    assert.ok(approval.seq < last(ops).seq, 'approval recorded before the activation');
    assert.deepEqual(
      { source: last(ops).payload.source, by: last(ops).payload.activatedBy },
      { source: 'code', by: 'human:alice' },
    );
    const active = activeFromLedger(ops)!;
    assert.equal(active.version, artifact.version);
    // Without --confirm-objective the objective travels unchanged.
    assert.equal(active.objective.status, OBJECTIVE_STATUS);
    assert.equal(active.objective.approvedBy, null);

    const next = await w.run();
    assert.notEqual(next.outcome, 'drift', next.failed.join('\n'));
    assert.equal(next.ledgerActive, artifact.version);
    assert.equal(next.broker?.active, artifact.version);
  } finally {
    await w.close();
  }
});

test('--confirm-objective persists the confirmation and who gave it', { skip: NEEDS_BIN }, async () => {
  const w = await bootstrapped();
  try {
    const { file } = await revision(w);
    const result = await publish(w, file, true);
    assert.equal(result.ok, true, result.message);
    const active = activeFromLedger(await w.store.ops(POLICY_ID))!;
    assert.deepEqual(
      { id: active.objective.outcomeId, dir: active.objective.direction, status: active.objective.status, by: active.objective.approvedBy },
      { id: 'job_wall_s', dir: 'min', status: CONFIRMED, by: 'human:alice' },
    );
    assert.deepEqual(active.guardrails.map((g) => [g.outcomeId, g.bound, g.status]),
      [['lease_expired', 0.01, CONFIRMED], ['caller_ok', 0.01, CONFIRMED]]);
    assert.notEqual((await w.run()).outcome, 'drift');
  } finally {
    await w.close();
  }
});

test('a revision not parented on the ledger\'s active artifact is refused', { skip: NEEDS_BIN }, async () => {
  const w = await bootstrapped();
  try {
    const before = (await w.store.ops(POLICY_ID)).length;
    const { file } = await revision(w, { parent_version: `b3:${'0'.repeat(64)}` });
    const result = await publish(w, file);
    assert.equal(result.ok, false);
    assert.match(result.message, /names parent b3:0+, but b3:[0-9a-f]+ is active/);
    assert.deepEqual(puts(w), []);
    assert.equal((await w.store.ops(POLICY_ID)).length, before, 'a refusal records nothing');
  } finally {
    await w.close();
  }
});

test('an artifact the binary rejects is refused before anything is recorded or published', { skip: NEEDS_BIN }, async () => {
  const w = await bootstrapped();
  try {
    const before = (await w.store.ops(POLICY_ID)).length;
    const { file, artifact } = await revision(w);
    // Out of the family's bounds, and a version the binary never computed.
    await writeFile(file, JSON.stringify({ ...artifact, params: { ...artifact.params, w_speed: -1e9 } }));
    const result = await publish(w, file);
    assert.equal(result.ok, false);
    assert.match(result.message, /artifact_invalid: .*param_out_of_bounds/);
    assert.deepEqual(puts(w), []);
    assert.equal((await w.store.ops(POLICY_ID)).length, before);
  } finally {
    await w.close();
  }
});

test('a failed PUT leaves the activation blocked with the reason; the ledger keeps the old artifact', { skip: NEEDS_BIN }, async () => {
  const w = await bootstrapped();
  try {
    const { file, artifact } = await revision(w);
    w.broker.failPut = new Error('broker said 503');
    const result = await publish(w, file, true);
    assert.equal(result.ok, false);
    assert.match(result.message, /activation blocked: publish_failed:broker said 503/);
    const ops = await w.store.ops(POLICY_ID);
    assert.equal(last(ops).opType, ACTIVATION_OPS.blocked);
    assert.equal(last(ops).payload.version, artifact.version);
    assert.match(String(last(ops).payload.reason), /publish_failed:broker said 503/);
    assert.equal(ops.filter((o) => o.opType === 'human_patch_approval').length, 1, 'who asked is still recorded');
    assert.equal(activeFromLedger(ops)?.version, w.meta.active.version);
    assert.equal(w.broker.active?.version, w.meta.active.version);
    w.broker.failPut = null;
    assert.notEqual((await w.run()).outcome, 'drift');
  } finally {
    await w.close();
  }
});

test('the CLI names the operator and the binary, or refuses', async () => {
  assert.equal(await main([], { USER: 'alice' }), 64);
  assert.equal(await main(['a.json', '--nope'], { USER: 'alice' }), 64);
  // Refused before any database or broker is touched.
  assert.equal(await main(['a.json'], {}), 2);
  assert.equal(await main(['a.json'], { USER: 'alice' }), 2);
});
