/**
 * A person's side of the improver (task 5.3; J§10.3: every promotion is approved by a
 * person, and nothing here runs on a timer).
 *
 *   npm run policy-approve -- <proposalId> --shadow     shadow it beside the active artifact
 *   npm run policy-approve -- <proposalId> --activate   make it active; refused below `shadow`
 *   npm run policy-revert  -- livestack.fleet.choose_target
 *
 * Every path first checks that the broker is deciding with what the activation ledger says
 * (the same reconcile as the improver's step 3): approving on top of drift would publish
 * against a state nobody recorded.
 *
 * Approval is recorded as a `human_patch_approval` entry in the activation ledger, naming
 * the operator (`$USER`), BEFORE anything is published — so a publish that fails still
 * leaves the record of who asked for it. Activation and revert then go through jingway's
 * activation service, which publishes first and records `active` only once the broker has
 * the artifact (a failed publish is recorded `blocked`, never `active`).
 */
import { rm } from 'node:fs/promises';
import path from 'node:path';

import { PolicyCli } from 'jingway-framework/server/policy';
import { runPolicyLadder, type RungResult } from 'jingway-framework/server/routine/improver/ladder.js';

import {
  brokerPublisher,
  httpPolicyBroker,
  readAdminToken,
  revertPublisher,
  type BrokerPolicyStatus,
  type PolicyBroker,
} from './broker.js';
import { DEFAULT_BROKER_URL, DEFAULT_FIXTURES } from './improver.js';
import { POLICY_ID } from './source.js';
import {
  activationsFromLedger,
  activeFromLedger,
  improverDbFromEnv,
  openImproverStore,
  proposalsByVersion,
  readProposal,
  setProposalStatus,
  shadowWindows,
  type ImproverStore,
  type LedgerActivation,
} from './store.js';

/** Shadow windows (days) a candidate needs before `--activate` (J§9.4 `shadowWindows`). */
export const DEFAULT_SHADOW_WINDOWS = 3;
/** Minimum effective sample size per guardrail per shadow window (tunePolicy's default). */
export const DEFAULT_SHADOW_MIN_ESS = 100;
const MAX_SHADOWS = 2;

export interface ApproveResult {
  ok: boolean;
  message: string;
  rungs?: RungResult[];
}

export interface ApproveOptions {
  store: ImproverStore;
  broker: PolicyBroker;
  /** Needed for `--activate` (the ladder's deterministic and cornerstones rungs). */
  cli?: PolicyCli;
  proposalId: string;
  mode: 'shadow' | 'activate';
  approver: string;
  invariantFixtures?: readonly string[];
  shadowWindows?: number;
  minEss?: number;
  now?: () => Date;
}

export async function approveProposal(o: ApproveOptions): Promise<ApproveResult> {
  const proposal = await readProposal(o.store, o.proposalId);
  if (!proposal) return refuse(`no proposal ${o.proposalId}`);
  if (proposal.status === 'activated') return refuse(`${o.proposalId} is already activated`);
  const policyId = proposal.policyId;
  const active = activeFromLedger(await o.store.ops(policyId));
  if (!active) return refuse(`${policyId} has no active artifact in the ledger; run the improver once to bootstrap it`);
  if (proposal.parentVersion !== active.version) {
    return refuse(`${o.proposalId} was tuned from ${proposal.parentVersion}, but ${active.version} is active now; `
      + 'its evidence is about a policy that is no longer running. Wait for a fresh proposal.');
  }
  const status = await o.broker.status(policyId);
  const drift = driftOf(active, status);
  if (drift) return refuse(drift);
  const at = (o.now ?? (() => new Date()))().toISOString();

  if (o.mode === 'shadow') {
    await o.store.appendApproval(policyId, {
      proposalId: o.proposalId, version: proposal.version, approver: o.approver, role: 'shadow', at,
    });
    // The broker's shadow file is replaced whole: keep what it already shadows (bodies from
    // our own proposals), newest last, at most two.
    const known = await proposalsByVersion(o.store, policyId);
    const keep = status.shadow.map((s) => known.get(s.version)).filter((p) => p && p.version !== proposal.version);
    const list = [...keep.map((p) => p!.artifact), proposal.artifact].slice(-MAX_SHADOWS);
    const dropped = status.shadow.map((s) => s.version).filter((v) => !list.some((a) => a.version === v));
    await o.broker.put(policyId, 'shadow', list);
    await setProposalStatus(o.store, o.proposalId, 'shadow');
    return {
      ok: true,
      message: `${o.proposalId} (${proposal.version}) is now shadowed; routing is unchanged.`
        + (dropped.length ? ` No longer shadowed: ${dropped.join(', ')}.` : ''),
    };
  }

  if (!o.cli) return refuse('--activate needs LIVESTACK_POLICY_BIN (the ladder validates through the binary)');
  const evidence = proposal.evidence as {
    evidence?: { selfCheck?: { self_check?: 'passed' | 'failed' | 'empty' } };
  };
  const windows = await shadowWindows(o.store, policyId, proposal.version);
  const workDir = path.join(o.store.root, 'scratch', `approve-${o.proposalId}-${process.pid}`);
  const rungs = await runPolicyLadder({
    cli: o.cli,
    parent: active.artifact,
    candidate: proposal.artifact,
    workDir,
    invariantFixtures: o.invariantFixtures ?? [DEFAULT_FIXTURES],
    admission: {
      selfCheck: evidence.evidence?.selfCheck?.self_check ?? null,
      // Stored only for a candidate tunePolicy admitted (improver step 6).
      admittedVersions: [proposal.version],
    },
    shadow: {
      source: { windows: async () => windows },
      minWindows: o.shadowWindows ?? DEFAULT_SHADOW_WINDOWS,
      guardrails: active.guardrails,
      minEss: o.minEss ?? DEFAULT_SHADOW_MIN_ESS,
    },
  }, 'shadow').finally(() => rm(workDir, { recursive: true, force: true }));
  const failed = rungs.find((r) => !r.passed);
  if (failed || rungs.length === 0 || rungs.at(-1)!.rung !== 'shadow') {
    return {
      ok: false,
      rungs,
      message: `refused: ${o.proposalId} has not passed the shadow rung — `
        + (failed ? `${failed.rung} failed (${failed.failure}): ${failed.detail ?? ''}` : 'the ladder stopped early'),
    };
  }

  await o.store.appendApproval(policyId, {
    proposalId: o.proposalId, version: proposal.version, approver: o.approver, role: 'active', approvedAtRung: 'shadow', at,
  });
  await o.store.saveArtifact(proposal.artifact);
  const record = await o.store.activation({ publisher: brokerPublisher(o.broker), actor: `human:${o.approver}` })
    .activatePolicy({
      artifact: proposal.artifact,
      objective: active.objective,
      guardrails: active.guardrails,
      source: 'improvement',
      sourceRef: o.proposalId,
    });
  if (record.status !== 'active') {
    return { ok: false, rungs, message: `activation ${record.status}: ${record.reason ?? ''}; the broker keeps ${active.version}` };
  }
  await setProposalStatus(o.store, o.proposalId, 'activated');
  return { ok: true, rungs, message: `${o.proposalId} (${proposal.version}) is active; it replaced ${active.version}.` };
}

export interface RevertOptions {
  store: ImproverStore;
  broker: PolicyBroker;
  policyId: string;
  approver: string;
}

/**
 * Back to the artifact that was active before the current one. The activation transition
 * is recorded through jingway's service with a publisher that is the broker's file-swap
 * revert (no model, nothing a bad policy can lock out), and only after checking the swap
 * would land on the version the ledger names — a revert that activated a file the ledger
 * never recorded would be drift manufactured by the tool meant to fix it.
 */
export async function revertPolicy(o: RevertOptions): Promise<ApproveResult> {
  const ops = await o.store.ops(o.policyId);
  const active = activeFromLedger(ops);
  if (!active) return refuse(`${o.policyId} has no active artifact in the ledger; nothing to revert`);
  const target = activationsFromLedger(ops).reverse().find((a) => a.seq < active.seq && a.version !== active.version);
  if (!target) return refuse(`${o.policyId} has had only one active artifact (${active.version}); nothing to revert to`);
  const status = await o.broker.status(o.policyId);
  const drift = driftOf(active, status);
  if (drift) return refuse(drift);
  if (status.previous?.version !== target.version) {
    return refuse(`the broker's previous artifact is ${status.previous?.version ?? 'none'}, but the ledger's previous is `
      + `${target.version}; a file-swap revert would activate something the ledger did not name. Activate ${target.version} explicitly instead.`);
  }
  const record = await o.store.activation({ publisher: revertPublisher(o.broker), actor: `human:${o.approver}` })
    .activatePolicy({
      artifact: target.artifact,
      objective: active.objective,
      guardrails: active.guardrails,
      source: target.source === 'improvement' ? 'improvement' : 'code',
      sourceRef: `revert:${active.version}`,
    });
  if (record.status !== 'active') {
    return refuse(`revert ${record.status}: ${record.reason ?? ''}; the broker keeps ${active.version}`);
  }
  return { ok: true, message: `${o.policyId} reverted to ${target.version} (was ${active.version}).` };
}

export function driftOf(active: LedgerActivation, status: BrokerPolicyStatus): string | null {
  if (status.source === 'file' && status.active.version === active.version) return null;
  return `policy_projection_drift: the activation ledger says ${active.version} is active; the broker `
    + (status.source === 'file' ? `decides with ${status.active.version}` : 'runs on compiled defaults')
    + '. Refusing to publish on top of a state nobody recorded.';
}

function refuse(message: string): ApproveResult {
  return { ok: false, message: `refused: ${message}` };
}

/**
 * `policy-approve <proposalId> --shadow|--activate [--broker URL] [--fixtures GLOB]`
 * `policy-approve --revert <policyId> [--broker URL]` (what `npm run policy-revert` runs).
 */
export async function main(argv: readonly string[], env: NodeJS.ProcessEnv = process.env): Promise<number> {
  type Mode = 'shadow' | 'activate' | 'revert';
  let mode: Mode | null = null;
  let target: string | null = null;
  let broker = DEFAULT_BROKER_URL;
  let fixtures = DEFAULT_FIXTURES;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--shadow' || a === '--activate' || a === '--revert') {
      if (mode) return usage(`${a} and --${mode} together`);
      mode = a.slice(2) as Mode;
    } else if (a === '--broker') broker = argv[++i];
    else if (a === '--fixtures') fixtures = argv[++i];
    else if (a.startsWith('--')) return usage(`unknown flag ${a}`);
    else if (target) return usage(`unexpected argument ${a}`);
    else target = a;
  }
  if (!mode || !target) return usage('a target and one of --shadow, --activate, --revert are required');
  const approver = env.USER;
  if (!approver) {
    console.error('refused: $USER is not set, and an approval must name the person who gave it');
    return 2;
  }
  const dbPath = improverDbFromEnv(env);
  const store = await openImproverStore({ dbPath, root: path.dirname(dbPath) });
  try {
    const client = httpPolicyBroker({ baseUrl: broker, token: await readAdminToken(env) });
    const result = mode === 'revert'
      ? await revertPolicy({ store, broker: client, policyId: target, approver })
      : await approveProposal({
        store,
        broker: client,
        cli: env.LIVESTACK_POLICY_BIN ? new PolicyCli(env.LIVESTACK_POLICY_BIN) : undefined,
        proposalId: target,
        mode,
        approver,
        invariantFixtures: [fixtures],
      });
    for (const r of result.rungs ?? []) console.log(`  ${r.rung}: ${r.passed ? 'passed' : `FAILED (${r.failure}) ${r.detail ?? ''}`}`);
    (result.ok ? console.log : console.error)(result.message);
    return result.ok ? 0 : 2;
  } finally {
    await store.close();
  }
}

function usage(problem: string): number {
  console.error(`${problem}\nusage: policy-approve <proposalId> --shadow|--activate [--broker URL] [--fixtures GLOB]\n`
    + `       policy-approve --revert ${POLICY_ID} [--broker URL]`);
  return 64;
}
