/**
 * Joining a weave's paper trail to the operation that paid for it.
 *
 * jingway records repairs and step summaries against a conversation; the broker
 * records claims and transitions against an `operation_id`. Neither knows the
 * other's key, and a retrospective that cannot join them can answer "what did
 * the loop do" and "what did the fleet spend" but never "what did this repair
 * turn cost" — which is the only question worth asking about an escalation.
 *
 * So the host is wrapped rather than the framework changed: every record,
 * summary and event leaves with `job_id`, `operation_id` and `run_id` attached.
 *
 * A throw in a sink is contained, deliberately. Losing a summary must not change
 * an outcome (jingway's invariant 7) — but it is REPORTED, because a paper trail
 * that goes quiet and a system that had nothing to say look identical from here.
 */
import type { RepairRecord } from 'jingway-framework/common/routines/contract.js';
import type { WeaveBlockSummary, WeaveEvent, WeaveHost } from 'jingway-framework/common/routines/weave.js';

export interface FleetTrace {
  job_id: string;
  run_id: string;
  operation_id?: string;
}

export type TraceSink = (entry: FleetTrace & { type: 'repair' | 'summary' | 'event'; payload: unknown }) => void;

export interface TracedHostOptions {
  base: WeaveHost;
  jobId: string;
  runId: string;
  /** Read at emit time, because the operation id does not exist until the claim. */
  operationId: () => string | undefined;
  sink: TraceSink;
  onSinkError?: (error: unknown) => void;
}

export function tracedHost(options: TracedHostOptions): WeaveHost {
  const { base, jobId, runId, operationId, sink } = options;
  const report = options.onSinkError ?? ((e: unknown) => console.error('[fleetd] trace sink failed:', e));
  const trace = (): FleetTrace => ({
    job_id: jobId,
    run_id: runId,
    ...(operationId() ? { operation_id: operationId()! } : {}),
  });
  const send = (type: 'repair' | 'summary' | 'event', payload: unknown) => {
    try {
      sink({ ...trace(), type, payload });
    } catch (error) {
      report(error);
    }
  };
  return {
    ...base,
    async record(record: RepairRecord) {
      send('repair', { ...record, ...trace() });
      await base.record(record);
    },
    emit(event: WeaveEvent) {
      send('event', event);
      base.emit(event);
    },
    ...(base.summarize || true
      ? {
          async summarize(summary: WeaveBlockSummary) {
            send('summary', summary);
            await base.summarize?.(summary);
          },
        }
      : {}),
  };
}
