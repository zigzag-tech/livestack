/**
 * Publish a HAND-AUTHORED artifact revision through the improver's activation ledger
 * (scheduler-policy-routine 7.1/7.2): the operator's path for a change no proposal made —
 * turning exploration on, or confirming the objective.
 *
 *   npm run policy-publish -- <artifact.json> [--confirm-objective] [--broker URL]
 *
 * Why not a raw `PUT`: the ledger is the authority for which artifact is active (design §1).
 * A PUT the ledger never recorded makes the broker's active version differ from the
 * ledger's, and every later improver run stops with `policy_projection_drift`.
 *
 * Order of operations — the same as `policy-approve --activate` (approve.ts):
 *   1. validate through `$LIVESTACK_POLICY_BIN artifact validate` (the binary computes the
 *      version; TypeScript never does), and refuse unless `parent_version` is the ledger's
 *      active version and the broker is deciding with that version (no drift);
 *   2. record a `human_patch_approval` naming the operator BEFORE anything is published, so
 *      a publish that fails still leaves the record of who asked for it;
 *   3. jingway's activation service publishes (`PUT role=active`) first and records second:
 *      `active` only once the broker has the artifact, `blocked` with the reason otherwise.
 *
 * The objective and guardrails are carried from the current activation (changing them is
 * not what an artifact revision does). `--confirm-objective` marks them confirmed and
 * `approvedBy: human:<operator>` (task 7.1).
 */
import { readFile } from 'node:fs/promises';
import path from 'node:path';

import { parsePolicyArtifact, PolicyCli, type PolicyArtifact } from 'jingway-framework/server/policy';

import { driftOf, type ApproveResult } from './approve.js';
import { brokerPublisher, httpPolicyBroker, readAdminToken, type PolicyBroker } from './broker.js';
import { DEFAULT_BROKER_URL } from './improver.js';
import {
  activeFromLedger,
  improverDbFromEnv,
  openImproverStore,
  type ImproverStore,
  type StoredFloor,
  type StoredObjective,
} from './store.js';

export const CONFIRMED = 'confirmed';

export interface PublishOptions {
  store: ImproverStore;
  broker: PolicyBroker;
  cli: PolicyCli;
  /** The artifact file, `version` already computed by the binary (`artifact hash`). */
  artifactPath: string;
  operator: string;
  confirmObjective?: boolean;
  now?: () => Date;
}

export async function publishArtifact(o: PublishOptions): Promise<ApproveResult> {
  let artifact: PolicyArtifact;
  try {
    artifact = parsePolicyArtifact(JSON.parse(await readFile(o.artifactPath, 'utf8')));
  } catch (error) {
    return refuse(`cannot read ${o.artifactPath} as a policy artifact: ${(error as Error).message}`);
  }
  const validation = await o.cli.validate(o.artifactPath);
  if (!validation.ok) {
    return refuse(`artifact_invalid: ${validation.violations.map((v) => `${v.code}: ${v.detail}`).join('; ')}`);
  }
  if (validation.version !== artifact.version) {
    return refuse(`the binary computes ${validation.version} for an artifact claiming ${artifact.version}`);
  }
  const policyId = artifact.policy_id;
  const active = activeFromLedger(await o.store.ops(policyId));
  if (!active) return refuse(`${policyId} has no active artifact in the ledger; run the improver once to bootstrap it`);
  if (artifact.parent_version !== active.version) {
    return refuse(`${artifact.version} names parent ${artifact.parent_version ?? 'none'}, but ${active.version} is active; `
      + 'a revision must be authored against the artifact it replaces');
  }
  if (artifact.version === active.version) return refuse(`${artifact.version} is already active`);
  const drift = driftOf(active, await o.broker.status(policyId));
  if (drift) return refuse(drift);

  const approvedBy = `human:${o.operator}`;
  const objective: StoredObjective = o.confirmObjective
    ? { ...active.objective, status: CONFIRMED, approvedBy }
    : active.objective;
  const guardrails: StoredFloor[] = o.confirmObjective
    ? active.guardrails.map((g) => ({ ...g, status: CONFIRMED }))
    : active.guardrails;
  const at = (o.now ?? (() => new Date()))().toISOString();

  await o.store.appendApproval(policyId, {
    proposalId: `hand:${artifact.version}`,
    version: artifact.version,
    approver: o.operator,
    role: 'active',
    source: 'code',
    confirmObjective: Boolean(o.confirmObjective),
    at,
  });
  await o.store.saveArtifact(artifact);
  const record = await o.store.activation({ publisher: brokerPublisher(o.broker), actor: approvedBy })
    .activatePolicy({ artifact, objective, guardrails, source: 'code', sourceRef: `hand:${o.operator}` });
  if (record.status !== 'active') {
    return { ok: false, message: `activation ${record.status}: ${record.reason ?? ''}; the broker keeps ${active.version}` };
  }
  return {
    ok: true,
    message: `${artifact.version} is active (hand-authored by ${approvedBy}); it replaced ${active.version}.`
      + (o.confirmObjective ? ` Objective ${objective.direction} ${objective.outcomeId} confirmed by ${approvedBy}.` : ''),
  };
}

function refuse(message: string): ApproveResult {
  return { ok: false, message: `refused: ${message}` };
}

/** `policy-publish <artifact.json> [--confirm-objective] [--broker URL]`. */
export async function main(argv: readonly string[], env: NodeJS.ProcessEnv = process.env): Promise<number> {
  let file: string | null = null;
  let confirmObjective = false;
  let broker = DEFAULT_BROKER_URL;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--confirm-objective') confirmObjective = true;
    else if (a === '--broker') broker = argv[++i];
    else if (a.startsWith('--')) return usage(`unknown flag ${a}`);
    else if (file) return usage(`unexpected argument ${a}`);
    else file = a;
  }
  if (!file) return usage('an artifact file is required');
  const operator = env.USER;
  if (!operator) {
    console.error('refused: $USER is not set, and a publish must name the person who made it');
    return 2;
  }
  if (!env.LIVESTACK_POLICY_BIN) {
    console.error('refused: LIVESTACK_POLICY_BIN is not set; the artifact is validated through the livestack-policy binary');
    return 2;
  }
  const dbPath = improverDbFromEnv(env);
  const store = await openImproverStore({ dbPath, root: path.dirname(dbPath) });
  try {
    const result = await publishArtifact({
      store,
      broker: httpPolicyBroker({ baseUrl: broker, token: await readAdminToken(env) }),
      cli: new PolicyCli(env.LIVESTACK_POLICY_BIN),
      artifactPath: path.resolve(file),
      operator,
      confirmObjective,
    });
    (result.ok ? console.log : console.error)(result.message);
    return result.ok ? 0 : 2;
  } finally {
    await store.close();
  }
}

function usage(problem: string): number {
  console.error(`${problem}\nusage: policy-publish <artifact.json> [--confirm-objective] [--broker URL]`);
  return 64;
}
