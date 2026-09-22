/**
 * Turning what actually went wrong into cases a classifier can be measured on.
 *
 * Two sources, deliberately:
 *
 * * the **fake-provider suite** (`node-py/tests/test_fleet_workers.py`), which
 *   produces every failure shape on purpose — lost replies, late completions,
 *   restarts mid-create, refusals, shortages. Synthetic, complete, and labelled
 *   by construction because the test states what it made happen.
 * * the **decision ledger**, which produces what the fleet really did. Fewer
 *   cases, unbalanced, and only partly labelled — but the only evidence about
 *   incidents nobody thought to simulate.
 *
 * `sourceKey` is what keeps a split honest. Every case derived from one
 * operation shares a key, so an operation that produced three observations
 * cannot put one in development and two in the holdout and call that three
 * independent looks. `freezeDecisionCases` groups on it.
 *
 * A label from the ledger alone is `agent_only` unless a person confirmed it,
 * and jingway's evaluator HOLDS a holdout containing one rather than qualifying
 * it. That is the intended behaviour, not an obstacle: a receipt built on the
 * incumbent's own opinion measures agreement, not correctness.
 */
import type { DecisionCase } from 'jingway-framework/server/decisions';

import { FAILURE_CLASSES, type FailureClass } from './classify.js';

/** One ledger `operation` record, as `GET /fleet/ledger` returns it. */
export interface LedgerRecord {
  decision: string;
  request?: { operation_id?: string; job_id?: string; owner?: string };
  outcome?: { state?: string; error?: { stage?: string; class?: string; code?: string } | null };
  reason?: string;
  ts?: number;
}

export interface CaseSource {
  /** Stable id for the incident: the operation it happened to. */
  operationId: string;
  /** The structured error as recorded. */
  error?: { stage?: string; class?: string; code?: string } | null;
  state?: string;
  /** Who labelled it, and how. Absent means the ledger's own class. */
  labelSource?: DecisionCase['labelSource'];
  expected?: FailureClass;
}

const CANDIDATES = Object.keys(FAILURE_CLASSES) as FailureClass[];

/**
 * The class a recorded incident is labelled with when nobody has looked at it.
 *
 * It is exactly the broker's own `error.class` when that is one of the five, and
 * `undefined` otherwise — never a guess. An unlabellable incident is DROPPED
 * from the corpus rather than assigned `needs_investigation`, because filling
 * the residual class with everything we could not classify would teach a
 * measurement that the residual class is the common one.
 */
export function ledgerLabel(error: CaseSource['error']): FailureClass | undefined {
  const cls = error?.class;
  return cls && (CANDIDATES as string[]).includes(cls) ? (cls as FailureClass) : undefined;
}

export function toCases(sources: readonly CaseSource[]): DecisionCase[] {
  const out: DecisionCase[] = [];
  for (const source of sources) {
    const expected = source.expected ?? ledgerLabel(source.error);
    if (!expected) continue;
    out.push({
      id: `${source.operationId}:${source.state ?? 'unknown'}`,
      // Every observation of one operation shares a split. Three looks at one
      // incident are one case observed three times.
      sourceKey: source.operationId,
      candidateIds: CANDIDATES,
      expectedCandidateId: expected,
      labelSource: source.labelSource ?? 'agent_only',
      classId: expected,
    });
  }
  return out;
}

export function casesFromLedger(records: readonly LedgerRecord[]): DecisionCase[] {
  return toCases(
    records
      .filter((r) => r.decision === 'operation' && r.outcome?.error)
      .map((r) => ({
        operationId: r.request?.operation_id ?? 'unknown',
        error: r.outcome?.error ?? null,
        state: r.outcome?.state,
      })),
  );
}

/**
 * What the corpus is missing, named.
 *
 * A report that only says "142 cases" hides the thing that decides whether a
 * qualification means anything: whether any class has too few cases to measure,
 * and whether the labels are anybody's but the incumbent's.
 */
export interface CorpusReport {
  total: number;
  byClass: Record<string, number>;
  bySourceGroups: number;
  /** Classes with fewer cases than the floor — a qualification cannot speak for these. */
  thin: string[];
  /** How many carry a label no person confirmed. */
  agentOnly: number;
}

export function describeCorpus(cases: readonly DecisionCase[], perClassFloor = 10): CorpusReport {
  const byClass: Record<string, number> = Object.fromEntries(CANDIDATES.map((c) => [c, 0]));
  let agentOnly = 0;
  const groups = new Set<string>();
  for (const c of cases) {
    byClass[c.expectedCandidateId] = (byClass[c.expectedCandidateId] ?? 0) + 1;
    if (c.labelSource === 'agent_only' || !c.labelSource) agentOnly += 1;
    groups.add(c.sourceKey);
  }
  return {
    total: cases.length,
    byClass,
    bySourceGroups: groups.size,
    thin: Object.entries(byClass)
      .filter(([, n]) => n < perClassFloor)
      .map(([id]) => id),
    agentOnly,
  };
}

/**
 * Rebuild the operation a ledger record was about, well enough to REPLAY the
 * incident packet against a classifier.
 *
 * It is a reconstruction and it says so: fields the ledger never carried come
 * back absent, not defaulted, so a replayed packet reads `none recorded` exactly
 * where the original did. A reconstruction that filled in plausible values would
 * evaluate the classifier on evidence it will never see in production.
 */
export function operationFromLedger(record: LedgerRecord): Record<string, unknown> | undefined {
  const id = record.request?.operation_id;
  if (!id || record.decision !== 'operation') return undefined;
  const req = (record.request ?? {}) as Record<string, unknown>;
  const out = record.outcome ?? {};
  return {
    operation_id: id,
    idempotency_key: req.idempotency_key ?? '',
    job_id: record.request?.job_id ?? '',
    kind: req.kind ?? '',
    owner: record.request?.owner ?? '',
    target_id: req.target_id ?? req.tier ?? '',
    state: out.state ?? 'unknown',
    created_at: record.ts ?? 0,
    updated_at: record.ts ?? 0,
    terminal: out.state === 'released' || out.state === 'rejected',
    provider: req.provider ?? null,
    plan_version: req.plan_version ?? null,
    provider_instance_id: (out as Record<string, unknown>).provider_instance_id ?? null,
    node_id: null,
    error: out.error ?? null,
    reason: record.reason ?? null,
    observability_degraded: false,
  };
}

export function operationsFromLedger(records: readonly LedgerRecord[]): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const record of records) {
    const op = operationFromLedger(record);
    // Last write wins: the newest record for an operation is the state it ended
    // in, which is the state the incident is about.
    if (op) out[String(op.operation_id)] = op;
  }
  return out;
}
