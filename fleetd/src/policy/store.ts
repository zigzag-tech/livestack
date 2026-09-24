/**
 * The improver's own durable state (design §1 "Activation ledger + proposals + artifact
 * store", §9 bounds): a PGLite database at `$LIVESTACK_POLICY_IMPROVER_DB`, plus plain files
 * beside it — `artifacts/<version>.json` (the replay CLI's `--artifact-store`) and
 * `proposals/<id>.md` (what a person reads).
 *
 * The broker never reads any of this. It is the AUTHORITY for which artifact should be
 * active; the broker's `.active.json` is a projection of it (design §1 "Authority").
 *
 * Tables, besides jingway's own (`routine_activations` among them):
 *  - `policy_activation_ledger`: every activation transition jingway's service emits
 *    (`appendOp`), in order, plus `human_patch_approval` records. Unbounded by count on
 *    purpose — activations are a few per week (§9: "activations: all").
 *  - `policy_proposals`: one row per `tune_policy` proposal (newest 365 per policy kept).
 *  - `policy_shadow_windows`: shadow evidence per (candidate, window) (newest 365 per policy).
 *  - `policy_improver_runs`: one row per run — the round ledger (newest 365 kept).
 */
import { mkdir, open, readFile, rm, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';

import { Kysely, sql } from 'kysely';
import type { Migration, MigrationProvider } from 'kysely/migration';
import { runFrameworkMigrations } from 'jingway-framework/adapters/kysely-pg/migrations/compatibility.js';
import {
  FrameworkMigrationProvider,
  MergedMigrationProvider,
} from 'jingway-framework/adapters/kysely-pg/migrations/index.js';
import { createPGliteDialect } from 'jingway-framework/adapters/kysely-pg/pglite-dialect.js';
import {
  ACTIVATION_OPS,
  activationEntityId,
  type RoutineActivation,
} from 'jingway-framework/common/routines/activation.js';
import type { PolicyFloor, PolicyObjective } from 'jingway-framework/server/policy';
import {
  createRoutineActivationService,
  type PolicyPublisher,
  type RoutineActivationService,
} from 'jingway-framework/server/routines/RoutineActivationService.js';
import type { DB } from 'jingway-framework/server/database/types.js';
import type { PolicyArtifact } from 'jingway-framework/server/policy';

export const REALM = 'global';
export const KEEP_PER_POLICY = 365;

/** An objective as stored in the activation payload, with where it came from. */
export type StoredObjective = PolicyObjective & { status?: string; approvedBy?: string | null };
export type StoredFloor = PolicyFloor & { status?: string };

const improverMigrations: Record<string, Migration> = {
  '20260924_001_policy_improver': {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- schema-agnostic DDL
    async up(db: Kysely<any>) {
      await db.schema
        .createTable('policy_activation_ledger')
        .ifNotExists()
        .addColumn('seq', 'bigserial', (c) => c.primaryKey())
        .addColumn('op_type', 'text', (c) => c.notNull())
        .addColumn('entity_id', 'text', (c) => c.notNull())
        .addColumn('payload', 'jsonb', (c) => c.notNull())
        // Stored, NOT unique: jingway keys activations by (op, version), so re-activating a
        // version (a revert to it) carries a key the ledger has already seen, and it is a
        // genuinely new transition that must be recorded. Order is `seq`.
        .addColumn('idempotency_key', 'text', (c) => c.notNull())
        .addColumn('recorded_at', 'timestamptz', (c) => c.notNull().defaultTo(sql`CURRENT_TIMESTAMP`))
        .execute();
      await db.schema.createIndex('policy_activation_ledger_entity').ifNotExists()
        .on('policy_activation_ledger').columns(['entity_id', 'seq']).execute();
      await db.schema
        .createTable('policy_proposals')
        .ifNotExists()
        .addColumn('id', 'text', (c) => c.primaryKey())
        .addColumn('policy_id', 'text', (c) => c.notNull())
        .addColumn('version', 'text', (c) => c.notNull())
        .addColumn('parent_version', 'text', (c) => c.notNull())
        /** proposed | shadow | activated */
        .addColumn('status', 'text', (c) => c.notNull())
        .addColumn('artifact', 'jsonb', (c) => c.notNull())
        .addColumn('evidence', 'jsonb', (c) => c.notNull())
        .addColumn('doc_path', 'text', (c) => c.notNull())
        .addColumn('created_at', 'timestamptz', (c) => c.notNull())
        .execute();
      await db.schema
        .createTable('policy_shadow_windows')
        .ifNotExists()
        .addColumn('policy_id', 'text', (c) => c.notNull())
        .addColumn('candidate_version', 'text', (c) => c.notNull())
        .addColumn('from_ts', 'double precision', (c) => c.notNull())
        .addColumn('to_ts', 'double precision', (c) => c.notNull())
        .addColumn('decisions', 'integer', (c) => c.notNull())
        .addColumn('agreement_rate', 'double precision', (c) => c.notNull())
        .addColumn('floors', 'jsonb', (c) => c.notNull())
        .addColumn('evaluated_at', 'timestamptz', (c) => c.notNull().defaultTo(sql`CURRENT_TIMESTAMP`))
        .addPrimaryKeyConstraint('policy_shadow_windows_pk', ['candidate_version', 'from_ts'])
        .execute();
      await db.schema
        .createTable('policy_improver_runs')
        .ifNotExists()
        .addColumn('id', 'bigserial', (c) => c.primaryKey())
        .addColumn('policy_id', 'text', (c) => c.notNull())
        .addColumn('started_at', 'timestamptz', (c) => c.notNull())
        .addColumn('outcome', 'text', (c) => c.notNull())
        /** Candidates compared this round: the multiplicity the next round inherits. */
        .addColumn('comparisons', 'integer', (c) => c.notNull())
        .addColumn('summary', 'jsonb', (c) => c.notNull())
        .execute();
    },
  },
};

class ImproverMigrations implements MigrationProvider {
  async getMigrations() {
    return { ...improverMigrations };
  }
}

export interface LedgerOp {
  seq: number;
  opType: string;
  payload: RoutineActivation & Record<string, unknown>;
  recordedAt: Date;
}

/** One `routine_activation.activated` transition, as the ledger recorded it. */
export interface LedgerActivation {
  seq: number;
  version: string;
  artifact: PolicyArtifact;
  objective: StoredObjective;
  guardrails: StoredFloor[];
  source: string;
  sourceRef: string;
  activatedAt: Date;
}

export interface ProposalRow {
  id: string;
  policyId: string;
  version: string;
  parentVersion: string;
  status: 'proposed' | 'shadow' | 'activated';
  artifact: PolicyArtifact;
  evidence: Record<string, unknown>;
  docPath: string;
  createdAt: Date;
}

export interface ShadowWindowRow {
  policyId: string;
  candidateVersion: string;
  from: Date;
  to: Date;
  decisions: number;
  agreementRate: number;
  floors: Array<{ outcome: string; ci: { lo: number; hi: number }; ess: number }>;
}

export interface ImproverStore {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- own tables beside jingway's DB type
  readonly db: Kysely<any>;
  /** Directory holding the database, artifacts/, proposals/ and scratch. */
  readonly root: string;
  readonly artifactDir: string;
  readonly proposalDir: string;
  /** jingway's activation service over this database, recording into the ledger table. */
  activation(options: { publisher?: PolicyPublisher; actor: string }): RoutineActivationService;
  ops(policyId: string): Promise<LedgerOp[]>;
  appendApproval(policyId: string, payload: Record<string, unknown>): Promise<void>;
  saveArtifact(artifact: PolicyArtifact): Promise<string>;
  close(): Promise<void>;
}

export function improverDbFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  return env.LIVESTACK_POLICY_IMPROVER_DB || path.join(homedir(), '.local/share/livestack/policy-improver/pg');
}

/**
 * Open (and migrate) the improver's database. `dbPath` null = in memory (tests).
 *
 * PGLite is one process's database. The improver's timer and a person's `policy-approve`
 * could otherwise open the same directory at once, so a lock file beside it refuses the
 * second opener instead of letting two processes write one data directory.
 */
export async function openImproverStore(options: { dbPath: string | null; root: string }): Promise<ImproverStore> {
  const root = options.root;
  const artifactDir = path.join(root, 'artifacts');
  const proposalDir = path.join(root, 'proposals');
  await mkdir(artifactDir, { recursive: true });
  await mkdir(proposalDir, { recursive: true });
  const releaseLock = await acquireLock(path.join(root, 'improver.lock'));

  let db: Kysely<unknown>;
  try {
    if (options.dbPath) await mkdir(options.dbPath, { recursive: true });
    const { dialect } = await createPGliteDialect(options.dbPath ? { dataDir: options.dbPath } : undefined);
    db = new Kysely({ dialect });
    await runFrameworkMigrations(db, {
      backend: 'pglite',
      provider: new MergedMigrationProvider(new FrameworkMigrationProvider(), new ImproverMigrations()),
    });
  } catch (error) {
    await releaseLock();
    throw error;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const anyDb = db as Kysely<any>;

  const registries = {
    // A compiled policy requires nothing from a realm (it runs in its host), so the
    // activation service is never asked to resolve a tool, sub-agent or document type here.
    hasTool: () => false,
    toolContractVersion: () => undefined,
    hasSubAgent: () => false,
    subAgentContractVersion: () => undefined,
    hasDocType: () => false,
    docTypeVersion: () => undefined,
  };

  return {
    db: anyDb,
    root,
    artifactDir,
    proposalDir,
    activation({ publisher, actor }) {
      return createRoutineActivationService({
        db: db as unknown as Kysely<DB>,
        registries,
        actor,
        policyPublisher: publisher,
        appendOp: async (op) => {
          await anyDb.insertInto('policy_activation_ledger').values({
            op_type: op.opType,
            entity_id: op.entityId,
            payload: JSON.stringify(op.payload),
            idempotency_key: op.idempotencyKey,
          }).execute();
        },
        log: (line) => console.error(line),
      });
    },
    async ops(policyId) {
      const rows = await anyDb.selectFrom('policy_activation_ledger').selectAll()
        .where('entity_id', '=', activationEntityId(REALM, policyId))
        .orderBy('seq').execute();
      return rows.map((r) => ({
        seq: Number(r.seq),
        opType: r.op_type as string,
        payload: (typeof r.payload === 'string' ? JSON.parse(r.payload) : r.payload) as LedgerOp['payload'],
        recordedAt: new Date(r.recorded_at as string),
      }));
    },
    async appendApproval(policyId, payload) {
      await anyDb.insertInto('policy_activation_ledger').values({
        op_type: 'human_patch_approval',
        entity_id: activationEntityId(REALM, policyId),
        payload: JSON.stringify(payload),
        idempotency_key: `human_patch_approval:${policyId}:${String(payload.proposalId)}:${String(payload.role)}`,
      }).execute();
    },
    async saveArtifact(artifact) {
      const file = path.join(artifactDir, `${artifact.version}.json`);
      await writeFile(file, JSON.stringify(artifact, null, 2));
      return file;
    },
    async close() {
      try {
        await db.destroy();
      } finally {
        await releaseLock();
      }
    },
  };
}

/**
 * The active artifact, derived from the activation LEDGER — never from the
 * `routine_activations` table.
 *
 * Why: that table keeps ONE row per (realm, policy), and `proposePolicy` writes a proposal
 * into that same row. After any proposal the row names the proposal's version with status
 * `proposed`, although the proposal is not active and the broker (correctly) still decides
 * with the old artifact. Reading "the active version" from the row would therefore report
 * `policy_projection_drift` after every proposal. The ledger keeps every transition, so the
 * active artifact is the latest `routine_activation.activated` whose version has not since
 * been retired or superseded (a `blocked` activation of a newer version leaves the older
 * one active: its publish failed, so the host never changed).
 */
export function activeFromLedger(ops: readonly LedgerOp[]): LedgerActivation | null {
  let active: LedgerActivation | null = null;
  for (const op of ops) {
    if (op.opType === ACTIVATION_OPS.activated) {
      active = toActivation(op);
    } else if (
      (op.opType === ACTIVATION_OPS.retired || op.opType === ACTIVATION_OPS.superseded)
      && active && op.payload.version === active.version
    ) {
      active = null;
    }
  }
  return active;
}

/** Every activation in order, oldest first (for revert and the hysteresis count). */
export function activationsFromLedger(ops: readonly LedgerOp[]): LedgerActivation[] {
  return ops.filter((op) => op.opType === ACTIVATION_OPS.activated).map(toActivation);
}

function toActivation(op: LedgerOp): LedgerActivation {
  const policy = op.payload.policy as { artifact: PolicyArtifact; objective: StoredObjective; guardrails: StoredFloor[] };
  return {
    seq: op.seq,
    version: op.payload.version,
    artifact: policy.artifact,
    objective: policy.objective,
    guardrails: policy.guardrails ?? [],
    source: op.payload.source,
    sourceRef: op.payload.sourceRef,
    activatedAt: new Date(op.payload.activatedAt),
  };
}

export async function readProposal(store: ImproverStore, id: string): Promise<ProposalRow | null> {
  const row = await store.db.selectFrom('policy_proposals').selectAll().where('id', '=', id).executeTakeFirst();
  return row ? toProposal(row) : null;
}

export async function proposalsByVersion(store: ImproverStore, policyId: string): Promise<Map<string, ProposalRow>> {
  const rows = await store.db.selectFrom('policy_proposals').selectAll().where('policy_id', '=', policyId).execute();
  return new Map(rows.map((r) => [r.version as string, toProposal(r)]));
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toProposal(r: any): ProposalRow {
  const json = (v: unknown) => (typeof v === 'string' ? JSON.parse(v) : v);
  return {
    id: r.id,
    policyId: r.policy_id,
    version: r.version,
    parentVersion: r.parent_version,
    status: r.status,
    artifact: json(r.artifact),
    evidence: json(r.evidence),
    docPath: r.doc_path,
    createdAt: new Date(r.created_at),
  };
}

export async function setProposalStatus(store: ImproverStore, id: string, status: ProposalRow['status']): Promise<void> {
  await store.db.updateTable('policy_proposals').set({ status }).where('id', '=', id).execute();
}

export async function shadowWindows(store: ImproverStore, policyId: string, version: string): Promise<ShadowWindowRow[]> {
  const rows = await store.db.selectFrom('policy_shadow_windows').selectAll()
    .where('policy_id', '=', policyId).where('candidate_version', '=', version).orderBy('from_ts').execute();
  return rows.map((r) => ({
    policyId: r.policy_id,
    candidateVersion: r.candidate_version,
    from: new Date(Number(r.from_ts) * 1000),
    to: new Date(Number(r.to_ts) * 1000),
    decisions: Number(r.decisions),
    agreementRate: Number(r.agreement_rate),
    floors: typeof r.floors === 'string' ? JSON.parse(r.floors) : r.floors,
  }));
}

export async function upsertShadowWindow(store: ImproverStore, w: ShadowWindowRow): Promise<void> {
  const values = {
    policy_id: w.policyId,
    candidate_version: w.candidateVersion,
    from_ts: w.from.getTime() / 1000,
    to_ts: w.to.getTime() / 1000,
    decisions: w.decisions,
    agreement_rate: w.agreementRate,
    floors: JSON.stringify(w.floors),
  };
  await store.db.insertInto('policy_shadow_windows').values(values)
    .onConflict((oc) => oc.columns(['candidate_version', 'from_ts']).doUpdateSet({
      to_ts: values.to_ts,
      decisions: values.decisions,
      agreement_rate: values.agreement_rate,
      floors: values.floors,
      evaluated_at: sql`CURRENT_TIMESTAMP`,
    }))
    .execute();
}

/**
 * §9 bounds, enforced at the end of every run: proposals (rows and documents), shadow
 * windows and run rows keep the newest `KEEP_PER_POLICY` per policy. Activations are all
 * kept. Returns what was deleted, so the summary can say so.
 */
export async function prune(store: ImproverStore, policyId: string, keep = KEEP_PER_POLICY): Promise<{
  proposals: number; shadowWindows: number; runs: number;
}> {
  const stale = await store.db.selectFrom('policy_proposals').select(['id', 'doc_path'])
    .where('policy_id', '=', policyId).orderBy('created_at', 'desc').offset(keep).limit(1_000_000).execute();
  for (const row of stale) await rm(row.doc_path as string, { force: true });
  if (stale.length > 0) {
    await store.db.deleteFrom('policy_proposals').where('id', 'in', stale.map((r) => r.id as string)).execute();
  }
  const windows = await sql<{ n: number }>`
    WITH doomed AS (
      SELECT candidate_version, from_ts FROM policy_shadow_windows WHERE policy_id = ${policyId}
      ORDER BY from_ts DESC OFFSET ${keep}
    )
    DELETE FROM policy_shadow_windows w USING doomed d
    WHERE w.candidate_version = d.candidate_version AND w.from_ts = d.from_ts
    RETURNING 1 AS n`.execute(store.db);
  const runs = await sql<{ n: number }>`
    DELETE FROM policy_improver_runs WHERE policy_id = ${policyId} AND id NOT IN (
      SELECT id FROM policy_improver_runs WHERE policy_id = ${policyId} ORDER BY id DESC LIMIT ${keep}
    ) RETURNING 1 AS n`.execute(store.db);
  return { proposals: stale.length, shadowWindows: windows.rows.length, runs: runs.rows.length };
}

export async function readJson<T>(file: string): Promise<T> {
  return JSON.parse(await readFile(file, 'utf8')) as T;
}

async function acquireLock(file: string): Promise<() => Promise<void>> {
  try {
    const handle = await open(file, 'wx');
    await handle.writeFile(String(process.pid));
    await handle.close();
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
    const holder = Number((await readFile(file, 'utf8').catch(() => '')).trim());
    if (holder && isAlive(holder)) {
      throw new Error(`the policy improver database is in use by pid ${holder} (${file})`);
    }
    // A lock left by a process that is gone: take it over, and say so.
    console.error(`[policy-improver] taking over stale lock ${file} (pid ${holder || 'unknown'} is not running)`);
    await writeFile(file, String(process.pid));
  }
  return async () => {
    await rm(file, { force: true });
  };
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}
