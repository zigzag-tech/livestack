#!/usr/bin/env -S npx tsx
/**
 * Run the frozen cases against a live classifier over a BALANCED schedule and
 * write the observations. Scoring is somebody else's job (jingway's
 * `scripts/evaluate-decisions.ts`), deliberately: an evaluator that also
 * collects can quietly drop the runs it did not like.
 *
 *   npx tsx scripts/observe-incidents.mts --cases receipts/cases.json \
 *       --classifier http://100.64.0.18:8188 --split development \
 *       --seed fleet-incident-seed-1 --out receipts/observations.json
 *
 * Every permutation is a CALL and every call is charged, including the repeats
 * a five-candidate schedule does not contain but a two-candidate one would.
 * Permutations are cost, never sample size.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import {
  DecisionClient,
  DecisionProfileRegistry,
  HarmonyClassifierAdapter,
  balancedSchedule,
  harmonySimpleJevChoiceProfile,
  scheduleBalance,
  type DecisionCase,
  type DecisionObservation,
  type FrozenDecisionCases,
} from 'jingway-framework/server/decisions';
import { classifyIncident, invariantViolations, type FailureClass } from '../src/classify.js';
import { buildIncidentPacket } from '../src/incident.js';

const args = new Map<string, string>();
for (let i = 2; i < process.argv.length; i += 2) args.set(process.argv[i]!.replace(/^--/, ''), process.argv[i + 1] ?? '');

const casesFile = args.get('cases');
const classifier = args.get('classifier');
if (!casesFile || !classifier) {
  console.error('usage: observe-incidents.mts --cases <file> --classifier <url> [--split development] [--seed s] --out <file>');
  process.exit(1);
}
const split = (args.get('split') ?? 'development') as keyof FrozenDecisionCases;
const seed = args.get('seed') ?? 'fleet-incident-seed-1';
const out = args.get('out') ?? 'receipts/observations.json';

const corpus = JSON.parse(readFileSync(casesFile, 'utf8')) as {
  frozen: FrozenDecisionCases;
  operations: Record<string, Record<string, unknown>>;
};
const { frozen, operations } = corpus;
const cases = (frozen[split] ?? []) as readonly DecisionCase[];
if (cases.length === 0) {
  console.error(`[fleetd] split '${String(split)}' is empty; nothing was written.`);
  process.exit(1);
}

const profile = harmonySimpleJevChoiceProfile({ contextTokens: Number(args.get('context') ?? 24_576) });
const profiles = new DecisionProfileRegistry();
profiles.register(profile);
const client = new DecisionClient({
  transport: new HarmonyClassifierAdapter({
    baseUrl: classifier,
    ...(process.env.HARMONY_OWNER ? { ownerScope: process.env.HARMONY_OWNER } : {}),
    ...(process.env.HARMONY_TOKEN ? { authorization: process.env.HARMONY_TOKEN } : {}),
  }),
  profiles,
  // NOT a real tokenizer. Replaced at the call site by the served model's own
  // count when the host can supply one; a 4-chars-per-token guess is recorded
  // in the receipt as an approximation rather than presented as a measurement.
  countTokens: (text) => Math.ceil(text.length / 4),
});

const schedule = balancedSchedule(cases[0]!.candidateIds, seed);
const balance = scheduleBalance(schedule);
if (!balance.balanced) {
  console.error(`[fleetd] the schedule is not balanced (${balance.distinctOrderings} distinct of ${schedule.length}); a truncated sweep is held, not scored.`);
  process.exit(1);
}

const observations: DecisionObservation[] = [];
for (const c of cases) {
  for (const permutation of schedule) {
    const operation = operations?.[c.sourceKey] as never;
    if (!operation) {
      // Skipped and SAID, never invented: a synthesised operation would evaluate
      // the classifier on evidence it will never be handed in production.
      console.error(`[fleetd] no operation snapshot for ${c.sourceKey}; skipped.`);
      continue;
    }
    const packet = buildIncidentPacket({ operation, plan: { plan_version: 'replay', policy: { digest: 'replay', weights: {} }, excluded: [], uncertainty: [], reservations: [] }, now: Date.now() / 1000 });
    const result = await classifyIncident({
      client,
      profile,
      packet,
      operation,
      mode: 'evaluation' as never,
      order: permutation.order as readonly FailureClass[],
      permutationId: permutation.id,
      seed,
      evidenceDigest: (p) => createHash('sha256').update(p.evidence.map((e) => e.text).join('\n')).digest('hex').slice(0, 16),
    });
    const selected = result.record.selected_candidate_id as FailureClass | undefined;
    observations.push({
      caseId: c.id,
      permutationId: permutation.id,
      outcome: result.record.outcome,
      ...(selected ? { selectedCandidateId: selected } : {}),
      elapsedMs: result.record.elapsed_ms ?? 0,
      requestCount: result.record.request_count ?? 0,
      branchCount: result.record.branch_count ?? 1,
      ...(result.record.max_option_probability !== undefined ? { maxOptionProbability: result.record.max_option_probability } : {}),
      // Recorded per observation: a selection a code invariant rejects is a
      // DANGEROUS-ACTION error, not a miss, and the two must not average.
      ...(selected ? { hardInvariantViolation: invariantViolations(selected, operation).length > 0 } : {}),
    });
  }
}

writeFileSync(out, `${JSON.stringify({ seed, split, schedule, observations }, null, 2)}\n`);
console.log(`[fleetd] ${observations.length} observation(s) over ${cases.length} case(s) x ${schedule.length} orderings -> ${out}`);
console.log('[fleetd] score it offline: npx tsx ~/jingway/scripts/evaluate-decisions.ts --cases … --candidate …');
