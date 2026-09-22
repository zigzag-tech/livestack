/**
 * The incident packet — everything a classifier (or a person) needs to say what
 * KIND of failure this was, and nothing it does not.
 *
 * Two rules shape it.
 *
 * **Versioned.** `INCIDENT_PACKET_VERSION` is part of the task version, so a
 * packet that gains a field is a different task and cannot inherit the old
 * one's qualification. A classifier evaluated on packets that carried the
 * region and then fed packets that do not is not the classifier that was
 * evaluated.
 *
 * **Required evidence refuses rather than trims.** Every item here marked
 * `required: true` is measured whole against the compiled prompt with the served
 * model's own tokenizer. If it does not fit, the decision is REFUSED with a
 * named reason and the incident goes to a person. Silently dropping the middle
 * of an error message produces an answer that looks exactly like an informed one
 * — which is the worst possible failure for a step whose entire job is to say
 * what happened.
 *
 * What is deliberately NOT in the packet: any credential, any token, any request
 * body. `owner` is an id, as it is everywhere else in this system.
 */
import type { EvidenceItem } from 'jingway-framework/server/decisions';
import type { FleetPlan, Operation } from './client.js';

export const INCIDENT_PACKET_VERSION = 'fleet-incident/v1';
export const INCIDENT_TASK_ID = 'fleet.failure_class';

/** How much of any one free-text field survives into the packet. */
export const MAX_FIELD_CHARS = 600;

export interface IncidentInput {
  operation: Operation;
  plan: Pick<FleetPlan, 'plan_version' | 'policy' | 'excluded' | 'uncertainty' | 'reservations'>;
  /** Unix seconds, so every age in the packet is absolute AND relative. */
  now: number;
  /** Slots held per owner, as the broker reported them. */
  usage?: Record<string, number>;
}

export interface IncidentPacket {
  version: string;
  /** Stable across re-observations of the same incident; part of the decision identity. */
  evidenceRevision: string;
  evidence: EvidenceItem[];
  /** For the record, not for the model. */
  subject: { operation_id: string; job_id: string; owner: string; target_id: string };
}

function clip(value: unknown): string {
  const text = value === null || value === undefined ? 'unknown' : String(value);
  return text.length > MAX_FIELD_CHARS ? `${text.slice(0, MAX_FIELD_CHARS)}…[clipped]` : text;
}

/**
 * `unknown` is spelled out, never defaulted to a number.
 *
 * A packet that renders an unreported `in_flight` as `0` tells the reader the
 * node is idle. It is the same defect as the one `fleet_ops_api` guards in the
 * plan, one layer further out, and it matters more here because the reader
 * cannot check.
 */
function say(label: string, value: unknown): string {
  return `${label}: ${clip(value)}`;
}

export function buildIncidentPacket(input: IncidentInput): IncidentPacket {
  const op = input.operation;
  const ageS = Math.max(0, Math.round(input.now - (op.created_at ?? input.now)));
  const sinceUpdateS = Math.max(0, Math.round(input.now - (op.updated_at ?? input.now)));

  const required: Array<[string, unknown]> = [
    ['packet_version', INCIDENT_PACKET_VERSION],
    ['operation_id', op.operation_id],
    ['job_id', op.job_id],
    ['kind', op.kind],
    ['owner', op.owner],
    ['target_pool', op.target_id],
    ['provider', op.provider],
    ['region', op.region],
    ['state', op.state],
    ['age_s', ageS],
    ['seconds_since_last_transition', sinceUpdateS],
    ['announce_deadline_passed', (op.announce_deadline ?? Infinity) < input.now],
    ['idempotency_key', op.idempotency_key],
    ['provider_instance_id', op.provider_instance_id ?? 'none recorded'],
    ['node_id', op.node_id ?? 'no node has announced this operation'],
    ['error_stage', op.error?.stage],
    ['error_class', op.error?.class],
    ['error_code', op.error?.code],
    ['error_excerpt', op.error?.excerpt],
    ['broker_reason', op.reason],
    ['plan_version', input.plan.plan_version],
    ['policy_digest', input.plan.policy?.digest],
    ['observability_degraded', op.observability_degraded],
  ];

  const optional: Array<[string, unknown]> = [
    [
      'excluded_targets',
      (input.plan.excluded ?? [])
        .map((e) => `${e.target_id ?? e.pool_id}: ${e.reason}`)
        .join(' | ') || 'none',
    ],
    [
      'uncertain_inputs',
      (input.plan.uncertainty ?? []).map((u) => u.reason).join(' | ') ||
        'none: every input the plan used was reported',
    ],
    [
      'outstanding_reservations',
      (input.plan.reservations ?? [])
        .map((r) => `${r.operation_id} ${r.state} on ${r.target_id}`)
        .join(' | ') || 'none',
    ],
    [
      'owner_slots_held',
      Object.entries(input.usage ?? {})
        .map(([owner, n]) => `${owner}=${n}`)
        .join(' ') || 'not reported',
    ],
  ];

  const evidence: EvidenceItem[] = [
    ...required.map(([label, value]) => ({ id: label, text: say(label, value), required: true })),
    ...optional.map(([label, value]) => ({ id: label, text: say(label, value), required: false })),
  ];

  return {
    version: INCIDENT_PACKET_VERSION,
    // The revision is what makes two observations of ONE incident the same case
    // and an incident that has moved a different one.
    evidenceRevision: `${op.operation_id}@${op.state}@${Math.round(op.updated_at ?? 0)}`,
    evidence,
    subject: {
      operation_id: op.operation_id,
      job_id: op.job_id,
      owner: op.owner,
      target_id: op.target_id,
    },
  };
}
