/**
 * The improver's test world: the committed fixture stream (written by the broker's real
 * write path — see scripts/gen-policy-fixture.py), a fake broker holding real policy files,
 * an in-memory improver database, and the REAL livestack-policy binary.
 *
 * The binary is not optional for the tests that replay: a replay "tested" against a stand-in
 * would pass whatever the compiled family does. Without `LIVESTACK_POLICY_BIN` those tests
 * are skipped with that sentence as the reason, never passed.
 */
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';
import { createGunzip } from 'node:zlib';

import { PolicyCli, type PolicyArtifact } from 'jingway-framework/server/policy';

import { FakePolicyBroker } from '../fakeBroker.js';
import { DEFAULT_FIXTURES, runImprover, type ImproverSummary, type TuneThresholds } from './improver.js';
import { POLICY_ID, StreamPolicySource } from './source.js';
import { openImproverStore, type ImproverStore } from './store.js';

export const BIN = process.env.LIVESTACK_POLICY_BIN;
/** `test(name, { skip: NEEDS_BIN }, …)`: false when the binary is there, the reason when not. */
export const NEEDS_BIN: string | false = BIN
  ? false
  : 'LIVESTACK_POLICY_BIN is not set: this test replays through the real livestack-policy binary '
    + '(cd native/policy && cargo build --release -p livestack-policy-cli)';

/** Thresholds a 600-decision fixture can meet. Production uses jingway's defaults. */
export const FIXTURE_THRESHOLDS: TuneThresholds = { minJoinedDecisions: 300, minRecords: 300, minEss: 10, resamples: 50 };

const FIXTURE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'stream');

export interface FixtureMeta {
  t0: number;
  decisions: number;
  self_principal: string;
  active: PolicyArtifact;
  shadow: PolicyArtifact;
}

export interface World {
  root: string;
  policyDir: string;
  meta: FixtureMeta;
  broker: FakePolicyBroker;
  store: ImproverStore;
  source: StreamPolicySource;
  cli: PolicyCli;
  now: Date;
  run(tune?: TuneThresholds): Promise<ImproverSummary>;
  close(): Promise<void>;
}

export async function world(options: { seedBroker?: boolean } = {}): Promise<World> {
  const root = await mkdtemp(path.join(tmpdir(), 'policy-improver-'));
  const policyDir = path.join(root, 'policy');
  const records = path.join(policyDir, 'records');
  await mkdir(records, { recursive: true });
  await pipeline(
    createReadStream(path.join(FIXTURE_DIR, `${POLICY_ID}.jsonl.gz`)),
    createGunzip(),
    createWriteStream(path.join(records, `${POLICY_ID}.jsonl`)),
  );
  const meta = JSON.parse(await readFile(path.join(FIXTURE_DIR, 'meta.json'), 'utf8')) as FixtureMeta;
  const broker = new FakePolicyBroker(policyDir, POLICY_ID);
  if (options.seedBroker !== false) broker.seed(meta.active);
  const store = await openImproverStore({ dbPath: null, root: path.join(root, 'improver') });
  const source = new StreamPolicySource({ dir: records });
  const cli = new PolicyCli(BIN ?? '/nonexistent/livestack-policy');
  // Five days after the first decision: the default 7-day window holds all 600.
  const now = new Date((meta.t0 + 5 * 86_400) * 1000);
  return {
    root,
    policyDir,
    meta,
    broker,
    store,
    source,
    cli,
    now,
    run: (tune) => runImprover({
      store,
      broker,
      source,
      cli,
      policyDir,
      invariantFixtures: [DEFAULT_FIXTURES],
      now: () => now,
      tune: { resamples: 50, ...tune },
    }),
    async close() {
      await store.close();
      await rm(root, { recursive: true, force: true });
    },
  };
}
