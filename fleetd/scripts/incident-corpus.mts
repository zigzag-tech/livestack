#!/usr/bin/env -S npx tsx
/**
 * Capture, freeze and describe the incident corpus.
 *
 *   npx tsx scripts/incident-corpus.mts --broker http://100.64.0.18:8801 \
 *       --since 0 --out receipts/cases.json
 *
 * Freezing FIRST is the point. The split is decided before anyone has seen a
 * score, and it is grouped by operation so an incident that produced three
 * observations cannot put one in development and two in the holdout. Scoring
 * happens later, offline, with jingway's evaluator — which is what makes a
 * published receipt checkable by someone who was not there.
 */
import { writeFileSync } from 'node:fs';
import { freezeDecisionCases } from 'jingway-framework/server/decisions';
import { casesFromLedger, describeCorpus, operationsFromLedger, type LedgerRecord } from '../src/corpus.js';

const args = new Map<string, string>();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i]!.replace(/^--/, ''), process.argv[i + 1] ?? '');
}
const broker = args.get('broker');
if (!broker) {
  console.error('usage: incident-corpus.mts --broker <url> [--since <unix>] [--limit 1000] --out <file>');
  process.exit(1);
}
const since = Number(args.get('since') ?? 0);
const limit = Number(args.get('limit') ?? 1000);
const out = args.get('out') ?? 'receipts/cases.json';

const response = await fetch(`${broker.replace(/\/+$/, '')}/fleet/ledger?since=${since}&limit=${limit}`);
if (!response.ok) {
  // Loud, and never an empty corpus: a freeze over zero cases would produce a
  // perfectly valid-looking file that qualifies nothing and says so nowhere.
  console.error(`[fleetd] ${broker}/fleet/ledger answered ${response.status}; nothing was written.`);
  process.exit(1);
}
const body = (await response.json()) as { records: LedgerRecord[] };
const cases = casesFromLedger(body.records ?? []);
if (cases.length === 0) {
  console.error('[fleetd] the ledger holds no labellable incident yet; nothing was written.');
  process.exit(1);
}
const report = describeCorpus(cases);
const frozen = freezeDecisionCases(cases);
// The operation snapshots travel WITH the cases. A replay needs the evidence,
// not just the label, and a corpus that carries only labels can be scored by
// nobody but the machine that captured it.
const operations = operationsFromLedger(body.records ?? []);
writeFileSync(out, `${JSON.stringify({ report, frozen, operations }, null, 2)}\n`);

console.log(`[fleetd] ${report.total} case(s) over ${report.bySourceGroups} operation(s) -> ${out}`);
console.log(`[fleetd] per class: ${JSON.stringify(report.byClass)}`);
if (report.thin.length) {
  console.log(`[fleetd] TOO THIN to speak for: ${report.thin.join(', ')} — a qualification cannot claim these.`);
}
if (report.agentOnly) {
  console.log(
    `[fleetd] ${report.agentOnly} case(s) carry a label no person confirmed. jingway's evaluator HOLDS a holdout ` +
      `containing one rather than qualifying it: a receipt built on the incumbent's own opinion measures agreement, not correctness.`,
  );
}
console.log(`[fleetd] splits: development ${frozen.development.length}, calibration ${frozen.calibration.length}, holdout ${frozen.holdout.length}`);
