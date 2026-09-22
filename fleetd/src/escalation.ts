/**
 * Where the classifier sits in the ladder: composed around a host, one rung
 * below the deterministic table and one above a full repair turn.
 *
 * The order is the design:
 *
 * 1. a registered workflow for this `{stage, class, code}` — zero tokens, and
 *    this host is never reached;
 * 2. **here**: one bounded Choice question, `failure_class`, over the model the
 *    fleet already serves;
 * 3. a full repair turn on the closed handback surface;
 * 4. `human_gate`.
 *
 * In `shadow` — the only mode this change ships — rung 2 RECORDS and rung 3
 * decides. That is not a formality. The selection is written with its order, its
 * probabilities and the workflow it WOULD have run, which is exactly the corpus
 * a qualification needs, and it is written while nothing depends on it being
 * right.
 *
 * One behaviour is not a placeholder even in shadow: an unreachable classifier
 * resolves to a durable human block and NOTHING is provisioned to restore it.
 * The classifier runs on the fleet's own LLM capacity. A loop that bursts to fix
 * its own classifier is a spending loop with an outage for a trigger.
 */
import type { RepairOutcome } from 'jingway-framework/common/routines/contract.js';
import type { EscalationPacket, WeaveHost } from 'jingway-framework/common/routines/weave.js';
import type { ToolCallingTool } from 'jingway-framework/server/subagent/runToolCallingSubAgent.js';

import type { Operation } from './client.js';
import type { ClassifyResult, IncidentDecisionRecord } from './classify.js';

export interface ClassifyingHostOptions {
  /** The host that runs the full repair turn. */
  base: WeaveHost;
  /** The operation this step is about, read at escalation time. */
  operationFor: (packet: EscalationPacket) => Operation | undefined;
  /** Rung 2. Returns undefined when there is nothing to classify. */
  classify: (operation: Operation, packet: EscalationPacket) => Promise<ClassifyResult | undefined>;
  /** Where the decision record goes. Called for EVERY attempt, including refusals. */
  persist: (record: IncidentDecisionRecord) => void | Promise<void>;
  onError?: (error: unknown) => void;
}

export function classifyingHost(options: ClassifyingHostOptions): WeaveHost {
  const report = options.onError ?? ((e: unknown) => console.error('[fleetd] classification failed:', e));
  return {
    ...options.base,
    async escalate(
      packet: EscalationPacket,
      tools: ToolCallingTool[],
      limits: { wallMs: number; maxSteps: number; signal: AbortSignal },
    ): Promise<RepairOutcome> {
      const operation = options.operationFor(packet);
      if (!operation) {
        // Nothing to classify. Straight to the repair turn, rather than asking
        // a model to reason about an incident with no subject.
        return options.base.escalate(packet, tools, limits);
      }
      let result: ClassifyResult | undefined;
      try {
        result = await options.classify(operation, packet);
      } catch (error) {
        // A classifier that THREW is an outage, not a verdict. It must not turn
        // into a silent fall-through that looks like a classifier which had
        // nothing to say.
        report(error);
        return {
          verdict: {
            verdict: 'human_gate',
            reason:
              `the incident classifier failed (${error instanceof Error ? error.message : String(error)}). ` +
              'It shares the fleet\'s LLM capacity; nothing was provisioned to restore it.',
          },
        };
      }
      if (result) {
        try {
          await options.persist(result.record);
        } catch (error) {
          // A lost record must not change an outcome, and must not be silent.
          report(error);
        }
        if (result.outcome.status === 'unavailable') {
          return {
            verdict: { verdict: 'human_gate', reason: result.outcome.reason },
            notes: `classifier outcome: ${result.record.outcome}`,
          };
        }
      }
      const outcome = await options.base.escalate(packet, tools, limits);
      return {
        ...outcome,
        notes: [outcome.notes, shadowNote(result)].filter(Boolean).join(' '),
      };
    },
  };
}

function shadowNote(result: ClassifyResult | undefined): string | undefined {
  if (!result) return undefined;
  switch (result.outcome.status) {
    case 'shadow':
      return `[shadow] failure_class=${result.outcome.failureClass} would have run ${result.outcome.workflow}; nothing was done with it.`;
    case 'rejected':
      return `[shadow] a code invariant rejected the classifier's winner and no runner-up was taken: ${result.outcome.violations.join('; ')}`;
    case 'abstained':
      return `[shadow] the classifier abstained: ${result.outcome.reason}`;
    case 'accepted':
      return `[shadow] failure_class=${result.outcome.failureClass}.`;
    default:
      return undefined;
  }
}
