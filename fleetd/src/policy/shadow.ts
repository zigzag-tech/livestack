/**
 * Shadow windows (improver step 7; J§10.2, J§9.4 `shadow` rung).
 *
 * While a candidate is shadowed, the broker records beside every decision what the
 * candidate would have chosen, greedily (`shadow: [{artifact_version, chosen}]`), and never
 * acts on it. This turns those records into the per-window evidence the ladder's `shadow`
 * rung reads (`PolicyShadowWindow`):
 *
 *  - `agreementRate`: the share of decisions where the candidate's greedy choice equals the
 *    active artifact's greedy choice (greedy against greedy: the active artifact's EXPLORED
 *    choices are the logging policy's, not its preference).
 *  - `floors`: per guardrail, the candidate's improvement over the active artifact on that
 *    window, positive = better. Estimated exactly as `tunePolicy` estimates lift: doubly
 *    robust under the difference target `sign·(q_candidate − q_active)`, with the plug-in
 *    reward model (the window's mean outcome per chosen candidate). Not SNIPS: a difference
 *    target's weights sum to about zero and SNIPS would divide by it. Not bare IPS: IPS is
 *    not invariant to a constant outcome, so a guardrail nobody ever breaks (every caller
 *    ok) would read as noise wide enough to fail the floor. `ess` is the candidate target's.
 *    Guardrails see ALL traffic, self-traffic included (jingway's stated default, J§14 q1).
 *
 * Windows are whole UTC days that ended inside the run's window; a day still in progress is
 * not evidence yet.
 */
import { dr, snips, toExposures, type LogEntry, type RewardModel } from 'jingway-framework/experiment';
import type { PolicyDecisionRecord, PolicyFloor, PolicyRecordSource } from 'jingway-framework/server/policy';

import type { ShadowWindowRow } from './store.js';

export const SHADOW_WINDOW_S = 86_400;

export interface ShadowEvaluationInput {
  source: PolicyRecordSource;
  policyId: string;
  /** Candidate versions the broker is shadowing now. */
  versions: readonly string[];
  window: { from: Date; to: Date };
  guardrails: readonly PolicyFloor[];
  seed: number;
  resamples?: number;
}

export async function evaluateShadowWindows(input: ShadowEvaluationInput): Promise<ShadowWindowRow[]> {
  if (input.versions.length === 0) return [];
  const log = await toExposures(input.source, {
    window: input.window,
    outcomeIds: input.guardrails.map((g) => g.outcomeId),
    includeSelfTraffic: true,
    unit: unitOf,
  });
  const outcomes = new Map(log.outcomes.map((o) => [o.exposureId, o]));

  const fromS = Math.ceil(input.window.from.getTime() / 1000 / SHADOW_WINDOW_S) * SHADOW_WINDOW_S;
  const endS = Math.floor(input.window.to.getTime() / 1000 / SHADOW_WINDOW_S) * SHADOW_WINDOW_S;
  const out: ShadowWindowRow[] = [];
  for (const version of input.versions) {
    for (let start = fromS; start + SHADOW_WINDOW_S <= endS; start += SHADOW_WINDOW_S) {
      const entries: Array<LogEntry & { shadowChosen: string | null; record: PolicyDecisionRecord }> = [];
      for (const exposure of log.exposures) {
        const record = log.records.get(exposure.exposureId)!;
        if (record.ts < start || record.ts >= start + SHADOW_WINDOW_S) continue;
        const shadow = record.shadow?.find((s) => s.artifact_version === version);
        if (!shadow) continue;
        entries.push({ exposure, outcome: outcomes.get(exposure.exposureId) ?? null, shadowChosen: shadow.chosen, record });
      }
      if (entries.length === 0) continue;
      const byId = new Map(entries.map((e) => [e.exposure.exposureId, e]));
      const agreed = entries.filter((e) => e.shadowChosen === e.record.greedy).length;
      const candidateQ = (id: string, c: string) => (byId.get(id)?.shadowChosen === c ? 1 : 0);
      const activeQ = (id: string, c: string) => (byId.get(id)?.record.greedy === c ? 1 : 0);
      const floors = input.guardrails.map((floor) => {
        const sign = floor.direction === 'max' ? 1 : -1;
        const options = { outcome: floor.outcomeId, seed: input.seed, resamples: input.resamples };
        const lift = dr(entries, (e) => Object.fromEntries(
          e.candidates.map((c) => [c.id, sign * (candidateQ(e.exposureId, c.id) - activeQ(e.exposureId, c.id))]),
        ), pluginModel(entries, floor.outcomeId), options);
        const ess = snips(entries, (e) => Object.fromEntries(
          e.candidates.map((c) => [c.id, candidateQ(e.exposureId, c.id)]),
        ), options).ess;
        return { outcome: floor.outcomeId, ci: lift.ci, ess };
      });
      out.push({
        policyId: input.policyId,
        candidateVersion: version,
        from: new Date(start * 1000),
        to: new Date((start + SHADOW_WINDOW_S) * 1000),
        decisions: entries.length,
        agreementRate: agreed / entries.length,
        floors,
      });
    }
  }
  return out;
}

/**
 * DR's plug-in reward model: the window's mean outcome by chosen candidate, falling back to
 * the window's mean. Supplied here, never fitted by the framework (J§9.3 step 4).
 */
function pluginModel(entries: readonly LogEntry[], outcomeId: string): RewardModel {
  const sums = new Map<string, { sum: number; n: number }>();
  const add = (key: string, value: number) => {
    const s = sums.get(key) ?? { sum: 0, n: 0 };
    s.sum += value;
    s.n += 1;
    sums.set(key, s);
  };
  for (const { exposure, outcome } of entries) {
    const value = outcome?.outcomes[outcomeId];
    if (value === undefined) continue;
    add(exposure.chosenId, value);
    add('\u0000all', value);
  }
  const mean = (key: string) => {
    const s = sums.get(key);
    return s && s.n > 0 ? s.sum / s.n : undefined;
  };
  return (_exposure, candidateId) => mean(candidateId) ?? mean('\u0000all') ?? 0;
}

/** Bootstrap clusters: the job (a retried job's decisions are not independent). */
export function unitOf(record: PolicyDecisionRecord): { kind: string; id: string } {
  const job = (record.context as { job?: { id?: unknown } } | null)?.job?.id;
  return typeof job === 'string' && job ? { kind: 'job', id: job } : { kind: 'decision', id: record.decision_id };
}
