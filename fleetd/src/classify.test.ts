/**
 * The classifier rung: what it is allowed to decide, and what happens when it
 * cannot.
 *
 * The properties under test are mostly refusals, because the value of this rung
 * is bounded by what it CANNOT do. A classifier that could pick a tier would be
 * a cheaper way to bypass the LAST_RESORT guard than any bug.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';

import {
  DecisionClient,
  DecisionProfileRegistry,
  harmonySimpleJevChoiceProfile,
  permutationIdentity,
  type AcceptancePolicy,
  type DecisionRequest,
  type DecisionTransport,
} from 'jingway-framework/server/decisions';

import {
  ACCEPTANCE_POLICY_VERSION,
  CLASS_TO_WORKFLOW,
  FAILURE_CLASSES,
  classifyIncident,
  failureCandidates,
  invariantViolations,
  type FailureClass,
} from './classify.js';
import { INCIDENT_PACKET_VERSION, buildIncidentPacket } from './incident.js';
import { NOW, err, operation, plan } from './fakeBroker.js';

const PROFILE = harmonySimpleJevChoiceProfile({ contextTokens: 24_576 });
const digest = (p: { evidence: Array<{ id: string; text: string }> }) =>
  createHash('sha256').update(p.evidence.map((e) => `${e.id}=${e.text}`).join('\n')).digest('hex').slice(0, 16);

function packetFor(op = operation({ state: 'failed', error: err('create', 'provider_fault', '503') })) {
  return { op, packet: buildIncidentPacket({ operation: op, plan: plan([]), now: NOW, usage: { acct_a: 1 } }) };
}

/** A transport that answers whatever the test tells it to, with receipts. */
function transport(answer: { pick?: FailureClass; status?: 'invalid_output' | 'unavailable' }): DecisionTransport {
  return {
    provider: 'harmony-simple-jev-v1',
    async execute(_profile: unknown, request: DecisionRequest) {
      const observation = {
        model: 'dbirks/Qwen3.8-27B-W4A16-AutoRound',
        templateVersion: 'v1',
        missingMetadata: ['cold_admission'],
        branchCount: request.questions.length,
      };
      if (answer.status === 'unavailable') {
        return { status: 'unavailable', violations: ['the classifier node is unreachable'], observation, retryable: false } as const;
      }
      if (answer.status === 'invalid_output') {
        return {
          status: 'invalid_output',
          violations: ["the permitted label 'C' was absent from the model's logprobs"],
          observation,
        } as const;
      }
      const order = request.permutation.order.failure_class!;
      const labels = ['A', 'B', 'C', 'D', 'E'];
      const labelMapping = Object.fromEntries(order.map((id, i) => [labels[i]!, id]));
      const pick = answer.pick ?? (order[0] as FailureClass);
      const probabilities = Object.fromEntries(order.map((id) => [id, id === pick ? 0.8 : 0.05]));
      return {
        status: 'answered',
        answers: [
          {
            questionId: 'failure_class',
            selectedCandidateId: pick,
            probabilities,
            maxOptionProbability: 0.8,
            labelMapping,
          },
        ],
        observation,
      } as const;
    },
  } as unknown as DecisionTransport;
}

function clientWith(t: DecisionTransport, acceptance?: AcceptancePolicy) {
  const profiles = new DecisionProfileRegistry();
  profiles.register(PROFILE);
  return new DecisionClient({
    transport: t,
    profiles,
    countTokens: (text) => Math.ceil(text.length / 4),
    ...(acceptance ? { acceptance } : {}),
  });
}

async function classify(
  t: DecisionTransport,
  over: Partial<Parameters<typeof classifyIncident>[0]> = {},
  subject = packetFor(),
  acceptance?: AcceptancePolicy,
) {
  return classifyIncident({
    client: clientWith(t, acceptance),
    profile: PROFILE,
    packet: subject.packet,
    operation: subject.op,
    mode: 'shadow',
    now: () => NOW,
    evidenceDigest: digest,
    ...over,
  });
}

// --- the packet -------------------------------------------------------------
test('the packet is versioned and names its version in its own evidence', () => {
  const { packet } = packetFor();
  assert.equal(packet.version, INCIDENT_PACKET_VERSION);
  assert.ok(packet.evidence.some((e) => e.text.includes(INCIDENT_PACKET_VERSION)));
});

test('the packet carries the whole structured error and the ids to join on', () => {
  const { packet } = packetFor();
  const byId = Object.fromEntries(packet.evidence.map((e) => [e.id, e.text]));
  for (const id of ['operation_id', 'job_id', 'owner', 'idempotency_key', 'error_stage', 'error_class', 'error_code', 'plan_version']) {
    assert.ok(byId[id], `${id} must be in the packet`);
  }
  assert.ok(packet.evidence.filter((e) => e.required).length >= 20);
});

test('an unreported field says unknown rather than rendering as zero', () => {
  const op = operation({ state: 'failed', provider_instance_id: null, node_id: null });
  const { packet } = packetFor(op);
  const byId = Object.fromEntries(packet.evidence.map((e) => [e.id, e.text]));
  assert.match(byId.provider_instance_id!, /none recorded/);
  assert.match(byId.node_id!, /no node has announced/);
});

test('the evidence revision moves when the incident moves, and not otherwise', () => {
  const a = packetFor(operation({ state: 'failed', updated_at: NOW })).packet.evidenceRevision;
  const b = packetFor(operation({ state: 'failed', updated_at: NOW })).packet.evidenceRevision;
  const c = packetFor(operation({ state: 'uncertain', updated_at: NOW })).packet.evidenceRevision;
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test('required evidence that does not fit REFUSES rather than being trimmed', async () => {
  const profiles = new DecisionProfileRegistry();
  const tiny = harmonySimpleJevChoiceProfile({ contextTokens: 10 });
  profiles.register(tiny);
  const client = new DecisionClient({ transport: transport({}), profiles, countTokens: (t) => Math.ceil(t.length / 4) });
  const subject = packetFor();
  const result = await classifyIncident({
    client,
    profile: tiny,
    packet: subject.packet,
    operation: subject.op,
    mode: 'shadow',
    now: () => NOW,
    evidenceDigest: digest,
  });
  assert.equal(result.outcome.status, 'unavailable');
  assert.match(result.raw.violations?.join(' ') ?? '', /token/);
});

// --- classify, not choose ---------------------------------------------------
test('every class maps to exactly one workflow, and none of them spend', () => {
  assert.deepEqual(Object.keys(CLASS_TO_WORKFLOW).sort(), Object.keys(FAILURE_CLASSES).sort());
  for (const workflow of Object.values(CLASS_TO_WORKFLOW)) {
    assert.ok(!['provision', 'deprovision', 'choose_tier'].includes(workflow));
  }
});

test('shadow records a selection and applies nothing', async () => {
  const result = await classify(transport({ pick: 'provider_fault' }));
  assert.equal(result.outcome.status, 'shadow');
  assert.equal(result.outcome.status === 'shadow' && result.outcome.failureClass, 'provider_fault');
  assert.equal(result.record.applied, false);
  assert.equal(result.record.executed_workflow, 'schedule_wakeup');
});

test('reversing the candidate order is a different observation of the same candidates', async () => {
  const forward = Object.keys(FAILURE_CLASSES) as FailureClass[];
  const reversed = [...forward].reverse();
  const a = await classify(transport({ pick: 'provider_fault' }), { order: forward });
  const b = await classify(transport({ pick: 'provider_fault' }), { order: reversed });
  assert.notEqual(a.record.submitted_order.join('>'), b.record.submitted_order.join('>'));
  assert.deepEqual([...a.record.submitted_order].sort(), [...b.record.submitted_order].sort());
  assert.notEqual(a.raw.permutation.id, b.raw.permutation.id);
  // Same case, different permutation identity: two observations, not two cases.
  assert.equal(a.record.evidence_revision, b.record.evidence_revision);
});

test('the record is enough to argue with a month later', async () => {
  const result = await classify(transport({ pick: 'capacity_shortage' }), {}, packetFor(
    operation({ state: 'rejected', terminal: true, error: err('create', 'capacity_shortage', 'no_capacity') }),
  ));
  const r = result.record;
  for (const field of [
    'decision_id', 'operation_id', 'job_id', 'task_id', 'task_version', 'profile_id',
    'evidence_revision', 'evidence_digest', 'submitted_order', 'label_mapping',
    'acceptance_policy_version', 'order_policy_version', 'outcome',
    'selected_candidate_id', 'probabilities', 'executed_workflow',
  ] as const) {
    assert.ok(r[field] !== undefined, `${field} must be persisted`);
  }
  assert.equal(r.acceptance_policy_version, ACCEPTANCE_POLICY_VERSION);
  assert.equal(r.model, 'dbirks/Qwen3.8-27B-W4A16-AutoRound');
  // Unobserved serving metadata is recorded as MISSING, never defaulted.
  assert.ok(r.missing_metadata.includes('cold_admission'));
  assert.equal(r.cost_usd, undefined, 'a resident local model is unpriced, not free');
});

// --- invariants -------------------------------------------------------------
test('an invariant rejection leaves NO selection and takes no runner-up', async () => {
  // The operation is `uncertain`; whatever else is true, the effect is unresolved.
  const subject = packetFor(operation({ state: 'uncertain', error: err('create', 'uncertain_effect', 'TimeoutError') }));
  const result = await classify(transport({ pick: 'provider_fault' }), {}, subject);
  assert.equal(result.outcome.status, 'rejected');
  assert.ok(result.record.invariant_violations.length > 0);
  assert.equal(result.record.executed_workflow, undefined);
  assert.equal(result.record.applied, false);
});

test('a class the evidence cannot support is rejected', () => {
  const noError = operation({ state: 'failed', error: null });
  assert.ok(invariantViolations('capacity_shortage', noError).length > 0);
  assert.equal(invariantViolations('needs_investigation', noError).length, 0);
  const atAnnounce = operation({ state: 'failed', error: err('announce', 'provider_fault', 'never_announced') });
  assert.match(invariantViolations('capacity_shortage', atAnnounce).join(' '), /impossible at stage 'announce'/);
});

test('a provider-stated refusal is not an uncertain effect', () => {
  const refusedOp = operation({ state: 'rejected', terminal: true, error: err('create', 'request_or_workload_fault', 'refused') });
  assert.match(invariantViolations('uncertain_effect', refusedOp).join(' '), /known to be nothing/);
});

// --- outages ----------------------------------------------------------------
test('an unreachable classifier is a durable human block and provisions nothing', async () => {
  const result = await classify(transport({ status: 'unavailable' }));
  assert.equal(result.outcome.status, 'unavailable');
  assert.equal(result.outcome.status === 'unavailable' && result.outcome.humanBlock, true);
  assert.match(
    result.outcome.status === 'unavailable' ? result.outcome.reason : '',
    /nothing is provisioned to restore it/,
  );
});

test('every non-answer keeps its own reason rather than collapsing into one word', async () => {
  const unavailable = await classify(transport({ status: 'unavailable' }));
  const invalid = await classify(transport({ status: 'invalid_output' }));
  const abstained = await classify(transport({ pick: 'provider_fault' }), {}, packetFor(), {
    version: 'test-abstain-v1',
    evaluate: () => ({ disposition: 'abstained' as const, reason: 'uncalibrated confidence below the qualification floor' }),
  });
  assert.equal(unavailable.record.outcome, 'unavailable');
  assert.equal(invalid.record.outcome, 'invalid_output');
  assert.equal(abstained.outcome.status, 'abstained');
  // An invalid output is not an outage: it means the permitted label was absent
  // from the logprobs, and quoting that is the whole point.
  assert.match(invalid.raw.violations?.join(' ') ?? '', /absent from the model/);
  assert.equal(unavailable.outcome.status, 'unavailable');
});

test('a serve request against an unqualified profile refuses before any dispatch', async () => {
  const result = await classify(transport({ pick: 'provider_fault' }), { mode: 'serve' });
  assert.equal(result.outcome.status, 'unavailable');
  assert.match(result.raw.violations?.join(' ') ?? '', /not qualified/);
});

test('the candidate descriptions are the criteria, verbatim', () => {
  const candidates = failureCandidates();
  assert.equal(candidates.length, 5);
  for (const c of candidates) {
    assert.equal(c.description, FAILURE_CLASSES[c.id as FailureClass]);
  }
  // Short and semantically distinct: the shape this transport is measured to
  // handle. Long near-identical strings are the shape it fails at.
  assert.ok(candidates.every((c) => c.description.length < 120));
});

test('permutationIdentity round-trips the order the record claims', () => {
  const order = Object.keys(FAILURE_CLASSES);
  const identity = permutationIdentity('fwd', 'seed', 'order-balanced-v1', { failure_class: order });
  assert.deepEqual(identity.order.failure_class, order);
});
