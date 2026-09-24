/**
 * `policy-improver`: one run of the scheduler policy's improver (task 5.2; design §1, §9;
 * J§9, J§10). Scheduled daily on the broker host (task 6.4); `npm run policy-improver -- --once`.
 *
 * Each run, in order, stopping at the first step whose precondition fails and naming it:
 *   1. open the improver's PGLite (`$LIVESTACK_POLICY_IMPROVER_DB`);
 *   2. bootstrap: with no activation for the policy yet, activate the artifact the broker is
 *      ALREADY deciding with, as hand-authored (`source: 'code'`) — verified, not re-PUT;
 *   3. reconcile: the broker's active version must equal the activation ledger's, else
 *      `policy_projection_drift` and stop (the ledger is the authority; the improver never
 *      overwrites the broker without an activation transition);
 *   4. window: the last 7 days;
 *   5. jingway `tunePolicy` over it, through the livestack-policy binary;
 *   6. a proposal climbs the ladder to `regression_corpus` and is written as a document a
 *      person reads (`proposals/<id>.md`) plus a `proposed` activation record;
 *   7. shadow windows are evaluated for every candidate the broker is shadowing;
 *   8. §9 pruning; the scratch directory is always deleted;
 *   9. a one-screen summary. A run that proposes nothing says why.
 *
 * Nothing here infers (design §8): search, replay and estimators only. Nothing here changes
 * routing either — publishing is `policy-approve`, a person's act.
 */
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  parsePolicyArtifact,
  PolicyCli,
  type PolicyArtifact,
} from 'jingway-framework/server/policy';
import { tunePolicy, type TunePolicyResult } from 'jingway-framework/server/routine/improver/tunePolicy.js';
import { runPolicyLadder, type RungResult } from 'jingway-framework/server/routine/improver/ladder.js';

import { alreadyPublished, httpPolicyBroker, type BrokerPolicyStatus, type PolicyBroker } from './broker.js';
import { evaluateShadowWindows, unitOf } from './shadow.js';
import { POLICY_ID, policyDirFromEnv, StreamPolicySource, type StreamReadStats } from './source.js';
import {
  activationsFromLedger,
  activeFromLedger,
  improverDbFromEnv,
  openImproverStore,
  prune,
  upsertShadowWindow,
  type ImproverStore,
  type LedgerActivation,
  type StoredFloor,
  type StoredObjective,
} from './store.js';

/**
 * Design §11 Q1, "proposed, pending a person": minimise the job's wall time, never let the
 * lease-expiry rate rise, or the callers' success rate fall, by more than 0.01 against the
 * incumbent. The status travels in the activation payload until a person confirms it
 * (task 7.1), so nobody mistakes a proposal for a decision.
 */
export const OBJECTIVE_STATUS = 'proposed, pending a person (scheduler-policy-routine design §11 Q1)';
export const DEFAULT_OBJECTIVE: StoredObjective = {
  outcomeId: 'job_wall_s',
  direction: 'min',
  status: OBJECTIVE_STATUS,
  approvedBy: null,
};
export const DEFAULT_GUARDRAILS: StoredFloor[] = [
  { outcomeId: 'lease_expired', direction: 'min', bound: 0.01, status: OBJECTIVE_STATUS },
  { outcomeId: 'caller_ok', direction: 'max', bound: 0.01, status: OBJECTIVE_STATUS },
];

export const IMPROVER_ACTOR = 'livestack:policy-improver';
export const DEFAULT_WINDOW_DAYS = 7;
export const DEFAULT_BROKER_URL = 'http://127.0.0.1:8801';
/** The family's invariant fixtures, in this repository (the `cornerstones` rung). */
export const DEFAULT_FIXTURES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../native/policy/family/tests/fixtures/livestack.fleet.choose_target/invariants/*.json',
);

export interface TuneThresholds {
  minJoinedDecisions?: number;
  minExploringFraction?: number;
  minEss?: number;
  minRecords?: number;
  maxCandidates?: number;
  resamples?: number;
}

export interface ImproverOptions {
  store: ImproverStore;
  broker: PolicyBroker;
  source: StreamPolicySource;
  cli: PolicyCli;
  /** `$LIVESTACK_POLICY_DIR`: the bootstrap reads the broker's active file from it. */
  policyDir: string;
  /** `replay --expect` globs for the cornerstones rung. Empty = no proposal can be made. */
  invariantFixtures: readonly string[];
  policyId?: string;
  now?: () => Date;
  windowDays?: number;
  tune?: TuneThresholds;
}

export type ImproverOutcome = 'proposed' | 'no_proposal' | 'drift' | 'blocked';

export interface ImproverSummary {
  policyId: string;
  startedAt: string;
  outcome: ImproverOutcome;
  /** Every precondition that failed, each naming itself. Empty only for a proposal. */
  failed: string[];
  bootstrapped: boolean;
  ledgerActive: string | null;
  broker: { source: string; active: string; previous: string | null; shadow: string[]; degraded: string[] } | null;
  window: { from: string; to: string } | null;
  stream: StreamReadStats | null;
  tune: { selfCheck: string | null; candidates: number; admitted: number; comparisons: number; alpha: number | null } | null;
  proposal: { id: string; version: string; doc: string; alreadyProposed: boolean } | null;
  shadow: Array<{ version: string; windows: number; decisions: number }>;
  pruned: { proposals: number; shadowWindows: number; runs: number } | null;
}

export async function runImprover(options: ImproverOptions): Promise<ImproverSummary> {
  const policyId = options.policyId ?? POLICY_ID;
  const now = (options.now ?? (() => new Date()))();
  const { store, broker, cli } = options;
  const summary: ImproverSummary = {
    policyId,
    startedAt: now.toISOString(),
    outcome: 'no_proposal',
    failed: [],
    bootstrapped: false,
    ledgerActive: null,
    broker: null,
    window: null,
    stream: null,
    tune: null,
    proposal: null,
    shadow: [],
    pruned: null,
  };
  const workDir = path.join(store.root, 'scratch', `run-${now.getTime()}-${process.pid}`);
  await mkdir(workDir, { recursive: true });
  try {
    const status = await broker.status(policyId);
    summary.broker = {
      source: status.source,
      active: status.active.version,
      previous: status.previous?.version ?? null,
      shadow: (status.shadow ?? []).map((s) => s.version),
      degraded: status.degraded ?? [],
    };

    // 2. Bootstrap.
    let active = activeFromLedger(await store.ops(policyId));
    if (!active) {
      const failure = await bootstrap(options, policyId, status, workDir);
      if (failure) {
        summary.failed.push(failure);
        summary.outcome = 'blocked';
        return summary;
      }
      summary.bootstrapped = true;
      active = activeFromLedger(await store.ops(policyId));
      if (!active) throw new Error('bootstrap reported success but the ledger has no active activation');
    }
    summary.ledgerActive = active.version;

    // 3. Reconcile. `active` comes from the LEDGER (see activeFromLedger for why not the
    // activation table, which a proposal overwrites).
    if (status.source !== 'file' || status.active.version !== active.version) {
      summary.failed.push(
        `policy_projection_drift: the activation ledger says ${active.version} is active; the broker `
          + (status.source === 'file' ? `decides with ${status.active.version}` : 'runs on compiled defaults')
          + '. Nothing was tuned or published; reconcile by an activation transition or a revert.',
      );
      summary.outcome = 'drift';
      return summary;
    }
    // The self-check replays each record under the artifact that logged it (J§7.3).
    await store.saveArtifact(active.artifact);

    // 4. Window.
    const windowDays = options.windowDays ?? DEFAULT_WINDOW_DAYS;
    const window = { from: new Date(now.getTime() - windowDays * 86_400_000), to: now };
    summary.window = { from: window.from.toISOString(), to: window.to.toISOString() };

    // 5. tune_policy.
    const result = await tune(options, active, window, workDir, windowDays);
    summary.stream = { ...options.source.lastRead };
    const minCoverage = 0.5;
    const comparisons = result.candidates.filter((c) => (c.supportCoverage ?? 0) >= minCoverage).length;
    summary.tune = {
      selfCheck: result.selfCheck?.self_check ?? null,
      candidates: result.candidates.length,
      admitted: result.candidates.filter((c) => c.admitted).length,
      comparisons,
      alpha: result.alpha,
    };
    await recordRun(store, policyId, now, comparisons, result.proposal ? 'proposed' : 'no_proposal');
    if (options.source.lastRead.missing.length > 0) {
      summary.failed.push(`stream incomplete: ${options.source.lastRead.missing.join(', ')} missing; treated as gaps`);
    }

    // 6. A proposal climbs the ladder, then becomes a document and a proposed record.
    if (!result.proposal) {
      summary.failed.push(`no proposal: ${result.reason ?? 'tunePolicy gave no reason'}`);
    } else {
      await propose(options, summary, active, result, workDir);
    }

    // 7. Shadow windows for whatever the broker shadows now.
    if (summary.broker.shadow.length > 0) {
      const rows = await evaluateShadowWindows({
        source: options.source,
        policyId,
        versions: summary.broker.shadow,
        window,
        guardrails: active.guardrails,
        seed: result.seed,
        resamples: options.tune?.resamples,
      });
      for (const row of rows) await upsertShadowWindow(store, row);
      for (const version of summary.broker.shadow) {
        const mine = rows.filter((r) => r.candidateVersion === version);
        summary.shadow.push({ version, windows: mine.length, decisions: mine.reduce((n, r) => n + r.decisions, 0) });
      }
    }

    // 8. Prune (§9).
    summary.pruned = await prune(store, policyId);
    return summary;
  } finally {
    await rm(workDir, { recursive: true, force: true });
  }
}

async function bootstrap(
  options: ImproverOptions,
  policyId: string,
  status: BrokerPolicyStatus,
  workDir: string,
): Promise<string | null> {
  if (status.source !== 'file') {
    return `policy_artifact_missing: the broker decides with compiled defaults (no ${policyId}.active.json), `
      + 'so there is no artifact to bootstrap the ledger from; a person publishes the first one (task 6.3)';
  }
  const file = path.join(options.policyDir, `${policyId}.active.json`);
  let artifact: PolicyArtifact;
  try {
    artifact = parsePolicyArtifact(JSON.parse(await readFile(file, 'utf8')));
  } catch (error) {
    return `bootstrap: cannot read the broker's active artifact ${file}: ${(error as Error).message}`;
  }
  if (artifact.version !== status.active.version) {
    return `bootstrap: ${file} holds ${artifact.version} but the broker reports ${status.active.version} active`;
  }
  // Validated from scratch before it enters the artifact store: the replay CLI loads every
  // file in the store, and one invalid file there would fail every later replay.
  const scratch = path.join(workDir, 'bootstrap.json');
  await writeFile(scratch, JSON.stringify(artifact));
  const validation = await options.cli.validate(scratch);
  if (!validation.ok) {
    return `bootstrap: artifact_invalid: ${validation.violations.map((v) => `${v.code}: ${v.detail}`).join('; ')}`;
  }
  if (validation.version !== artifact.version) {
    return `bootstrap: the binary computes ${validation.version} for an artifact claiming ${artifact.version}`;
  }
  await options.store.saveArtifact(artifact);
  const record = await options.store
    .activation({ publisher: alreadyPublished(options.broker), actor: IMPROVER_ACTOR })
    .activatePolicy({
      artifact,
      objective: DEFAULT_OBJECTIVE,
      guardrails: DEFAULT_GUARDRAILS,
      source: 'code',
      sourceRef: `policy:${policyId}`,
    });
  return record.status === 'active' ? null : `bootstrap activation ${record.status}: ${record.reason ?? ''}`;
}

async function tune(
  options: ImproverOptions,
  active: LedgerActivation,
  window: { from: Date; to: Date },
  workDir: string,
  windowDays: number,
): Promise<TunePolicyResult> {
  const { store } = options;
  const policyId = active.artifact.policy_id;
  const activations = activationsFromLedger(await store.ops(policyId));
  const lastTune = [...activations].reverse().find((a) => a.source === 'improvement') ?? null;
  const windowMs = windowDays * 86_400_000;
  // The round ledger: comparisons already spent on this policy since its last tune_policy
  // promotion, so multiplicity accumulates across rounds instead of resetting nightly.
  const prior = await store.db.selectFrom('policy_improver_runs')
    .select((eb) => eb.fn.coalesce(eb.fn.sum<number>('comparisons'), eb.lit(0)).as('n'))
    .where('policy_id', '=', policyId)
    .where('started_at', '>', lastTune?.activatedAt ?? new Date(0))
    .executeTakeFirst();
  const t = options.tune ?? {};
  return tunePolicy({
    cli: options.cli,
    source: options.source,
    active: active.artifact,
    objective: { outcomeId: active.objective.outcomeId, direction: active.objective.direction },
    guardrails: active.guardrails.map((g) => ({ outcomeId: g.outcomeId, direction: g.direction, bound: g.bound })),
    window,
    records: [options.source.pattern],
    artifactStore: store.artifactDir,
    workDir: path.join(workDir, 'tune'),
    unit: unitOf,
    windowsSinceTunePromotion: lastTune
      ? Math.floor((window.to.getTime() - lastTune.activatedAt.getTime()) / windowMs)
      : null,
    priorComparisons: Number(prior?.n ?? 0),
    maxCandidates: t.maxCandidates,
    minEss: t.minEss,
    minRecords: t.minRecords,
    minJoinedDecisions: t.minJoinedDecisions,
    minExploringFraction: t.minExploringFraction,
    resamples: t.resamples,
    now: options.now,
  });
}

async function propose(
  options: ImproverOptions,
  summary: ImproverSummary,
  active: LedgerActivation,
  result: TunePolicyResult,
  workDir: string,
): Promise<void> {
  const proposal = result.proposal!;
  const { store } = options;
  if (options.invariantFixtures.length === 0) {
    summary.failed.push('no proposal: the cornerstones rung has no invariant fixtures to run (pass --fixtures)');
    return;
  }
  const rungs = await runPolicyLadder({
    cli: options.cli,
    parent: active.artifact,
    candidate: proposal.artifact,
    workDir: path.join(workDir, 'ladder'),
    invariantFixtures: options.invariantFixtures,
    admission: {
      selfCheck: result.selfCheck?.self_check ?? null,
      admittedVersions: result.candidates.filter((c) => c.admitted).map((c) => c.artifactVersion),
    },
  }, 'regression_corpus');
  const failedRung = rungs.find((r) => !r.passed);
  if (failedRung) {
    summary.failed.push(`no proposal: ${proposal.artifact.version} failed the ${failedRung.rung} rung `
      + `(${failedRung.failure}): ${failedRung.detail ?? ''}`);
    return;
  }

  const id = proposalId(proposal.artifact.version);
  const doc = path.join(store.proposalDir, `${id}.md`);
  const existing = await store.db.selectFrom('policy_proposals').select('id').where('id', '=', id).executeTakeFirst();
  summary.outcome = 'proposed';
  summary.proposal = { id, version: proposal.artifact.version, doc, alreadyProposed: Boolean(existing) };
  if (existing) return;

  await store.saveArtifact(proposal.artifact);
  await writeFile(doc, renderProposal(id, active, result, rungs));
  await store.db.insertInto('policy_proposals').values({
    id,
    policy_id: proposal.policyId,
    version: proposal.artifact.version,
    parent_version: proposal.parentVersion,
    status: 'proposed',
    artifact: JSON.stringify(proposal.artifact),
    evidence: JSON.stringify({
      evidence: proposal.evidence,
      counterfactual: proposal.counterfactual,
      ladder: rungs,
      window: summary.window,
    }),
    doc_path: doc,
    created_at: new Date(summary.startedAt),
  }).execute();
  // The `proposed` activation record (jingway's ledger shape). NOTE: this overwrites the
  // activation TABLE's single row for the policy; the ledger keeps the active one, which is
  // why reconcile reads the ledger.
  await store.activation({ actor: IMPROVER_ACTOR }).proposePolicy({
    artifact: proposal.artifact,
    objective: active.objective,
    guardrails: active.guardrails,
    source: 'improvement',
    sourceRef: id,
  });
}

/** Stable per artifact version, short enough to type. */
export function proposalId(version: string): string {
  return `tp-${version.replace(/^b3:/, '').slice(0, 16)}`;
}

function renderProposal(id: string, active: LedgerActivation, result: TunePolicyResult, rungs: RungResult[]): string {
  const p = result.proposal!;
  const e = p.evidence.evaluation;
  const fmt = (n: number | null | undefined, d = 4) => (n === null || n === undefined ? 'n/a' : n.toFixed(d));
  const changed = Object.keys(p.artifact.params)
    .filter((k) => p.artifact.params[k] !== active.artifact.params[k])
    .map((k) => `| \`${k}\` | ${active.artifact.params[k]} | ${p.artifact.params[k]} |`);
  const outcome = (o: NonNullable<typeof e.objective>) =>
    `| \`${o.outcomeId}\` | ${fmt(o.lift.value)} | [${fmt(o.correctedCi.lo)}, ${fmt(o.correctedCi.hi)}] | ${fmt(o.snips.ess, 1)} |`;
  return [
    `# Proposal \`${id}\` — \`${p.policyId}\``,
    '',
    `\`tune_policy\` proposes replacing the active artifact \`${p.parentVersion}\` with \`${p.artifact.version}\`.`,
    'Nothing has changed on the broker. A person decides:',
    '',
    '```',
    `npm run policy-approve -- ${id} --shadow     # shadow it beside the active artifact (changes no routing)`,
    `npm run policy-approve -- ${id} --activate   # only after it has passed the shadow rung`,
    '```',
    '',
    '## Parameters that change',
    '',
    '| param | active | proposed |',
    '|---|---|---|',
    ...changed,
    '',
    `## Evidence (window ${result.facts.joinedDecisions} joined decisions, exploring ${fmt(result.facts.exploringFraction, 3)})`,
    '',
    `Objective: ${active.objective.direction} \`${active.objective.outcomeId}\`${active.objective.status ? ` — ${active.objective.status}` : ''}.`,
    'Lift is the improvement over the incumbent (positive = better), doubly robust; the interval is',
    `multiplicity-corrected (alpha ${fmt(p.evidence.alpha, 5)} over ${p.evidence.comparisons} comparisons).`,
    '',
    '| outcome | lift | corrected interval | ESS |',
    '|---|---|---|---|',
    ...(e.objective ? [outcome(e.objective)] : []),
    ...e.guardrails.map(outcome),
    '',
    `- Replay self-check: **${p.evidence.selfCheck.self_check}** (${p.evidence.selfCheck.checked} records checked)`,
    `- Support coverage: ${fmt(e.supportCoverage, 3)}; agreement with the incumbent: ${fmt(e.agreementRate, 3)}`,
    `- Seed ${p.evidence.seed} (${p.evidence.seedSource})`,
    `- Excluded: ${JSON.stringify(p.evidence.excluded)}`,
    '',
    '## Ladder',
    '',
    ...rungs.map((r) => `- \`${r.rung}\`: **${r.passed ? 'passed' : 'failed'}**${r.detail ? ` — ${r.detail}` : ''}`),
    '- `shadow`: **not run** — needs `--shadow` and shadow windows on live traffic',
    '',
    '## Where it would choose differently',
    '',
    ...(p.counterfactual.disagreements.length === 0
      ? ['No recorded decision in the window would change.']
      : p.counterfactual.disagreements.slice(0, 20).map((d) => `- \`${d}\``)),
    '',
  ].join('\n');
}

async function recordRun(store: ImproverStore, policyId: string, now: Date, comparisons: number, outcome: string) {
  await store.db.insertInto('policy_improver_runs').values({
    policy_id: policyId,
    started_at: now,
    outcome,
    comparisons,
    summary: JSON.stringify({ outcome, comparisons }),
  }).execute();
}

/** The one-screen summary. Every failed precondition is on it. */
export function formatSummary(s: ImproverSummary): string {
  const lines = [
    `policy-improver ${s.policyId} @ ${s.startedAt}: ${s.outcome.toUpperCase()}`,
    `  ledger active : ${s.ledgerActive ?? 'none'}${s.bootstrapped ? ' (bootstrapped this run, hand-authored)' : ''}`,
    s.broker
      ? `  broker        : ${s.broker.source} ${s.broker.active}; previous ${s.broker.previous ?? 'none'}; `
        + `shadow [${s.broker.shadow.join(', ')}]${s.broker.degraded.length ? `; degraded ${s.broker.degraded.join(', ')}` : ''}`
      : '  broker        : not read',
  ];
  if (s.window) lines.push(`  window        : ${s.window.from} .. ${s.window.to}`);
  if (s.stream) {
    lines.push(`  stream        : ${s.stream.files.length} file(s), ${s.stream.decisions} decisions, ${s.stream.outcomes} outcomes, `
      + `${s.stream.gaps} gap(s), ${s.stream.unparseable} unparseable${s.stream.missing.length ? `, MISSING ${s.stream.missing.join(', ')}` : ''}`);
  }
  if (s.tune) {
    lines.push(`  tune_policy   : self-check ${s.tune.selfCheck ?? 'not run'}; ${s.tune.candidates} candidates, `
      + `${s.tune.comparisons} compared, ${s.tune.admitted} admitted`);
  }
  if (s.proposal) {
    lines.push(`  proposal      : ${s.proposal.id} (${s.proposal.version})${s.proposal.alreadyProposed ? ' — already proposed' : ''}`);
    lines.push(`                  ${s.proposal.doc}`);
  }
  for (const sh of s.shadow) lines.push(`  shadow        : ${sh.version}: ${sh.windows} window(s), ${sh.decisions} decisions`);
  if (s.pruned) lines.push(`  pruned        : ${s.pruned.proposals} proposals, ${s.pruned.shadowWindows} shadow windows, ${s.pruned.runs} runs`);
  if (s.failed.length > 0) {
    lines.push('  failed:');
    for (const f of s.failed) lines.push(`    - ${f}`);
  }
  return lines.join('\n');
}

/** `npm run policy-improver -- --once [--broker URL] [--fixtures GLOB] [--window-days N]`. */
export async function main(argv: readonly string[], env: NodeJS.ProcessEnv = process.env): Promise<number> {
  const args = parseArgs(argv);
  if (!args.once) {
    console.error('usage: policy-improver --once [--broker URL] [--fixtures GLOB] [--window-days N]\n'
      + '  One run per invocation; the systemd timer schedules it. There is no daemon mode.');
    return 64;
  }
  const bin = env.LIVESTACK_POLICY_BIN;
  if (!bin) {
    console.error('LIVESTACK_POLICY_BIN is not set: the improver replays through the livestack-policy binary and cannot run without it');
    return 1;
  }
  const dbPath = improverDbFromEnv(env);
  const store = await openImproverStore({ dbPath, root: path.dirname(dbPath) });
  try {
    const summary = await runImprover({
      store,
      broker: httpPolicyBroker({ baseUrl: args.broker ?? DEFAULT_BROKER_URL }),
      source: StreamPolicySource.fromEnv(env),
      cli: new PolicyCli(bin),
      policyDir: policyDirFromEnv(env),
      invariantFixtures: [args.fixtures ?? DEFAULT_FIXTURES],
      windowDays: args.windowDays,
    });
    console.log(formatSummary(summary));
    return summary.outcome === 'drift' || summary.outcome === 'blocked' ? 2 : 0;
  } finally {
    await store.close();
  }
}

function parseArgs(argv: readonly string[]): { once: boolean; broker?: string; fixtures?: string; windowDays?: number } {
  const out: { once: boolean; broker?: string; fixtures?: string; windowDays?: number } = { once: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--once') out.once = true;
    else if (a === '--broker') out.broker = argv[++i];
    else if (a === '--fixtures') out.fixtures = argv[++i];
    else if (a === '--window-days') out.windowDays = Number(argv[++i]);
    else throw new Error(`unknown argument ${a}`);
  }
  return out;
}
