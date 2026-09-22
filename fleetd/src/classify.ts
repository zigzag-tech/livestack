/**
 * The Simple Jev rung — a CLASSIFIER, not a chooser.
 *
 * It answers exactly one bounded question about an incident nobody registered a
 * workflow for: *what kind of failure was this?* Code then maps the accepted
 * class to exactly one registered workflow. The model never names a tier, never
 * provisions, never touches region, quota or budget.
 *
 * That split is the whole design, and it is not squeamishness about models. A
 * one-token answer that could select `LAST_RESORT` would bypass the
 * lexicographic guard in `fleet_scheduler.schedule()` — the guard that makes
 * "last resort" literal rather than merely expensive. Diagnosis and spending
 * authority are different powers, and the measured behaviour of the transport
 * argues for keeping them apart: on four short, distinct triage labels this
 * mechanism shows ~10% order instability (jingway `docs/decision-models.md`).
 * Ten percent is usable for "which workflow should run"; it is not usable for
 * "should we rent the expensive thing".
 *
 * Three more things this file refuses to do, each because the alternative is
 * worse than no answer:
 *
 * * **No runner-up.** When a code invariant rejects the winner, the result
 *   carries `violations` and NO selection. Taking second place would mean code
 *   overruling a model with a guess.
 * * **No acting in shadow.** `shadow` records a selection and returns
 *   `applied: false`; the deterministic path handles the incident.
 * * **No recursive provisioning.** A classifier that is unreachable resolves to
 *   a durable human block. The classifier runs on the fleet's own LLM capacity;
 *   provisioning to restore it is how an outage becomes a spending loop.
 */
import {
  DecisionClient,
  newDecisionRequestId,
  permutationIdentity,
  type ChoiceCandidate,
  type DecisionAcceptance,
  type DecisionMode,
  type DecisionProfile,
  type DecisionRequest,
  type DecisionResult,
} from 'jingway-framework/server/decisions';

import type { Operation } from './client.js';
import { INCIDENT_PACKET_VERSION, INCIDENT_TASK_ID, type IncidentPacket } from './incident.js';
import type { WorkflowName } from './workflows.js';

export const FAILURE_CLASS_QUESTION_ID = 'failure_class';

/**
 * The five classes, with the criteria the compiler renders verbatim.
 *
 * Short and semantically distinct on purpose: that is the shape this transport
 * is measured to handle. Five near-identical long strings is the shape it fails
 * at (87.5% order instability), and the difference is not effort — it is the
 * task.
 */
export const FAILURE_CLASSES = {
  capacity_shortage: 'The provider had no capacity for this pool at this time.',
  request_or_workload_fault: 'Our request or workload was wrong: the spec, a credential, the image, or a quota.',
  provider_fault: 'The provider misbehaved: a 5xx, a timeout after it accepted, or an inconsistent state.',
  uncertain_effect: 'A create may have happened; the reply was lost or ambiguous.',
  needs_investigation: 'None of the above fits, or the evidence contradicts itself.',
} as const;

export type FailureClass = keyof typeof FAILURE_CLASSES;

/** Code's mapping, not the model's. One class, one workflow. */
export const CLASS_TO_WORKFLOW: Record<FailureClass, WorkflowName> = {
  capacity_shortage: 'refresh_availability',
  request_or_workload_fault: 'investigate',
  provider_fault: 'schedule_wakeup',
  uncertain_effect: 'reconcile_operation',
  needs_investigation: 'investigate',
};

export const ACCEPTANCE_POLICY_VERSION = 'fleet-incident-acceptance/v1';
export const ORDER_POLICY_VERSION = 'order-balanced-v1';
/** Bumped whenever the packet, the classes, the invariants or the mapping move. */
export const TASK_VERSION = `${INCIDENT_PACKET_VERSION}+classes/v1+invariants/v1`;

export function failureCandidates(order?: readonly FailureClass[]): ChoiceCandidate[] {
  const ids = order ?? (Object.keys(FAILURE_CLASSES) as FailureClass[]);
  return ids.map((id) => ({ id, description: FAILURE_CLASSES[id] }));
}

/**
 * Hard invariants. Code's, not the model's, and they never pick a replacement.
 *
 * Each one encodes something the evidence already SETTLES, so the model cannot
 * contradict it: if the provider stated a refusal, the effect is not unknown; a
 * shortage cannot have happened at a stage where nothing was requested; and
 * there is nothing to classify when no structured error was recorded.
 */
export function invariantViolations(selected: FailureClass, op: Operation): string[] {
  const out: string[] = [];
  if (!op.error && selected !== 'needs_investigation') {
    out.push(
      `no structured error was recorded for ${op.operation_id}, so nothing supports ${selected}; only needs_investigation is available`,
    );
  }
  if (selected === 'capacity_shortage' && op.error && op.error.stage !== 'create') {
    out.push(
      `capacity_shortage is impossible at stage '${op.error.stage}': nothing was requested from the provider there`,
    );
  }
  if (selected === 'uncertain_effect' && op.state === 'rejected' && op.error?.class === 'request_or_workload_fault') {
    out.push(
      `the provider stated a refusal (${op.error.code}); its effect is known to be nothing, so it is not uncertain`,
    );
  }
  if (selected !== 'uncertain_effect' && op.state === 'uncertain') {
    out.push(
      `${op.operation_id} is in state 'uncertain'; whatever else is true, the create's effect is unresolved and must be reconciled first`,
    );
  }
  return out;
}

export type ClassificationOutcome =
  | { status: 'accepted'; failureClass: FailureClass; workflow: WorkflowName; applied: boolean }
  | { status: 'shadow'; failureClass: FailureClass; workflow: WorkflowName; applied: false }
  | { status: 'rejected'; violations: string[] }
  | { status: 'abstained'; reason: string }
  | { status: 'unavailable'; reason: string; humanBlock: true };

/**
 * Everything needed to argue with this decision a month later, joined by
 * `operation_id` and `decision_id`.
 *
 * Deliberately flat and deliberately complete. A trace that records the answer
 * but not the ORDER cannot be re-run; one that records the order but not the
 * acceptance policy cannot explain why a valid answer was refused; one that
 * records neither the workflow nor the eventual outcome is a log line.
 */
export interface IncidentDecisionRecord {
  decision_id: string;
  operation_id: string;
  job_id: string;
  task_id: string;
  task_version: string;
  profile_id: string;
  mode: DecisionMode;
  evidence_revision: string;
  evidence_digest: string;
  /** The exact submitted order, which is what makes the observation reproducible. */
  submitted_order: readonly string[];
  label_mapping?: Readonly<Record<string, string>>;
  acceptance_policy_version: string;
  order_policy_version: string;
  outcome: DecisionResult['outcome'];
  selected_candidate_id?: string;
  probabilities?: Readonly<Record<string, number>>;
  max_option_probability?: number;
  acceptance?: DecisionAcceptance;
  invariant_violations: string[];
  executed_workflow?: WorkflowName;
  applied: boolean;
  /** Identity fields the service did not supply. Missing, never defaulted. */
  missing_metadata: readonly string[];
  model?: string;
  template_version?: string;
  elapsed_ms?: number;
  request_count?: number;
  branch_count?: number;
  /** Absent means NOT PRICED. A resident local model is unpriced, not free. */
  cost_usd?: number;
  at: number;
}

export interface ClassifyOptions {
  client: DecisionClient;
  profile: DecisionProfile;
  packet: IncidentPacket;
  operation: Operation;
  /** `shadow` records and acts on nothing. `serve` requires a qualified profile. */
  mode: DecisionMode;
  /** The submitted candidate order. Predeclared by the caller's schedule. */
  order?: readonly FailureClass[];
  permutationId?: string;
  seed?: string;
  deadlineMs?: number;
  now?: () => number;
  evidenceDigest: (packet: IncidentPacket) => string;
  signal?: AbortSignal;
}

export interface ClassifyResult {
  outcome: ClassificationOutcome;
  record: IncidentDecisionRecord;
  raw: DecisionResult;
}

export async function classifyIncident(options: ClassifyOptions): Promise<ClassifyResult> {
  const now = options.now ?? (() => Date.now() / 1000);
  const order = options.order ?? (Object.keys(FAILURE_CLASSES) as FailureClass[]);
  const candidates = failureCandidates(order);
  const requestId = newDecisionRequestId();
  const permutation = permutationIdentity(
    options.permutationId ?? order.join('>'),
    options.seed ?? 'fleet-incident',
    ORDER_POLICY_VERSION,
    { [FAILURE_CLASS_QUESTION_ID]: order },
  );
  const request: DecisionRequest = {
    requestId,
    taskId: INCIDENT_TASK_ID,
    taskVersion: TASK_VERSION,
    profileId: options.profile.id,
    mode: options.mode,
    evidence: options.packet.evidence,
    evidenceRevision: options.packet.evidenceRevision,
    questions: [
      {
        id: FAILURE_CLASS_QUESTION_ID,
        primitive: 'choice',
        instructions:
          'A fleet provisioning operation did not reach a usable machine. Using only the evidence, say what KIND of failure this was. Do not propose an action.',
        candidates,
      },
    ],
    deadlineMs: options.deadlineMs ?? 15_000,
    attempt: 1,
    permutation,
    // Local only: the incident packet describes this fleet's own machines and
    // accounts, and it never leaves it.
    caller: { locality: 'local-only' },
  };

  const raw = await options.client.decide(request, options.signal);
  const answer = raw.answers?.find((a) => a.questionId === FAILURE_CLASS_QUESTION_ID);
  const base: IncidentDecisionRecord = {
    decision_id: requestId,
    operation_id: options.packet.subject.operation_id,
    job_id: options.packet.subject.job_id,
    task_id: INCIDENT_TASK_ID,
    task_version: TASK_VERSION,
    profile_id: options.profile.id,
    mode: options.mode,
    evidence_revision: options.packet.evidenceRevision,
    evidence_digest: options.evidenceDigest(options.packet),
    submitted_order: order,
    acceptance_policy_version: ACCEPTANCE_POLICY_VERSION,
    order_policy_version: ORDER_POLICY_VERSION,
    outcome: raw.outcome,
    invariant_violations: [],
    applied: false,
    missing_metadata: raw.execution?.missingMetadata ?? ['execution'],
    ...(raw.execution?.model ? { model: raw.execution.model } : {}),
    ...(raw.execution?.templateVersion ? { template_version: raw.execution.templateVersion } : {}),
    ...(raw.execution ? { elapsed_ms: raw.execution.elapsedMs, request_count: raw.execution.requestCount, branch_count: raw.execution.branchCount } : {}),
    ...(raw.execution?.costUsd !== undefined ? { cost_usd: raw.execution.costUsd } : {}),
    ...(raw.acceptance ? { acceptance: raw.acceptance } : {}),
    ...(answer ? { label_mapping: answer.labelMapping, probabilities: answer.probabilities, max_option_probability: answer.maxOptionProbability, selected_candidate_id: answer.selectedCandidateId } : {}),
    at: now(),
  };

  // Every non-answer keeps its OWN reason. `unavailable`, `deadline_exceeded`,
  // `invalid_output`, `abstained` and `cancelled` want different responses, and
  // a caller handed one word for all five will retry the ones it must not.
  if (raw.outcome !== 'answered' || !answer) {
    if (raw.outcome === 'abstained') {
      return { raw, record: base, outcome: { status: 'abstained', reason: raw.acceptance?.reason ?? 'the policy declined this answer' } };
    }
    return {
      raw,
      record: base,
      outcome: {
        status: 'unavailable',
        humanBlock: true,
        reason:
          `${raw.outcome}` +
          (raw.violations?.length ? `: ${raw.violations.join('; ')}` : '') +
          '. The classifier shares the fleet\'s own LLM capacity, so nothing is provisioned to restore it.',
      },
    };
  }

  if (raw.acceptance?.disposition !== 'accepted') {
    return { raw, record: base, outcome: { status: 'abstained', reason: raw.acceptance?.reason ?? 'not accepted by the acceptance policy' } };
  }

  const selected = answer.selectedCandidateId as FailureClass;
  const violations = invariantViolations(selected, options.operation);
  if (violations.length) {
    // No selection, and no runner-up. Taking second place would be code
    // overruling a model with a guess.
    return { raw, record: { ...base, invariant_violations: violations }, outcome: { status: 'rejected', violations } };
  }

  const workflow = CLASS_TO_WORKFLOW[selected];
  if (options.mode !== 'serve') {
    return {
      raw,
      record: { ...base, executed_workflow: workflow, applied: false },
      outcome: { status: 'shadow', failureClass: selected, workflow, applied: false },
    };
  }
  return {
    raw,
    record: { ...base, executed_workflow: workflow, applied: true },
    outcome: { status: 'accepted', failureClass: selected, workflow, applied: true },
  };
}
