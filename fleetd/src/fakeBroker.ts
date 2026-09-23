/**
 * A fake control plane that misbehaves the way the real one does.
 *
 * It counts calls, because the properties worth asserting here are mostly about
 * what did NOT happen: no plan call after an unreadable view, no operation call
 * after a malformed plan, no escalation on the green path, no model on a
 * failure the table already knows.
 */
import {
  BrokerUnreachable,
  OperationRefused,
  type ActingAction,
  type FleetClient,
  type FleetPlan,
  type FleetView,
  type Operation,
  type PlanAction,
  type StructuredError,
} from './client.js';

export const NOW = 1_700_000_000;

export function view(overrides: Partial<FleetView> = {}): FleetView {
  return {
    generated_at: NOW,
    hosts: { h1: { nodes: [{ peer: 'http://a/livestack', state: 'fresh', ready: true, unseen_seconds: 0, kinds: ['llm'] }] } },
    ...overrides,
  };
}

export function plan(actions: PlanAction[], overrides: Partial<FleetPlan> = {}): FleetPlan {
  const jobs = actions.flatMap((a) => ('job_id' in a && a.job_id ? [{ job_id: a.job_id, kind: 'llm', owner: 'acct_a', sla: 'normal' }] : []));
  return {
    api: 'v1',
    plan_version: 'v1.abc.1700000000',
    generated_at: NOW,
    jobs,
    pools: [],
    excluded: [],
    uncertainty: [],
    reservations: [],
    policy: { digest: 'abc', weights: { resource: 1, budget: 1, speed: 1 } },
    actions,
    ...overrides,
  };
}

export function operation(over: Partial<Operation> = {}): Operation {
  return {
    operation_id: 'OP1',
    idempotency_key: 'k1',
    job_id: 'job-0',
    kind: 'llm',
    owner: 'acct_a',
    target_id: 'heyuan-spot',
    state: 'creating',
    created_at: NOW,
    updated_at: NOW,
    terminal: false,
    announce_deadline: NOW + 900,
    observability_degraded: false,
    ...over,
  };
}

export function err(stage: string, cls: string, code: string): StructuredError {
  return { stage, class: cls, code, excerpt: '' };
}

export interface FakeBrokerOptions {
  view?: FleetView | Error;
  plan?: FleetPlan | Error;
  /** What `startOperation` returns, or throws. */
  start?: Operation | Error;
  /** Successive answers from `operation()`; the last one repeats. */
  observations?: Operation[];
}

export class FakeBroker implements FleetClient {
  readonly calls: string[] = [];
  constructor(private readonly options: FakeBrokerOptions = {}) {}

  private record<T>(name: string, value: T | Error | undefined, fallback: T): Promise<T> {
    this.calls.push(name);
    if (value instanceof Error) return Promise.reject(value);
    return Promise.resolve(value ?? fallback);
  }

  view(): Promise<FleetView> {
    return this.record('view', this.options.view, view());
  }

  plan(): Promise<FleetPlan> {
    return this.record('plan', this.options.plan, plan([]));
  }

  startOperation(input: { action: ActingAction }): Promise<Operation> {
    this.calls.push(`startOperation:${input.action.type}`);
    const value = this.options.start;
    if (value instanceof Error) return Promise.reject(value);
    return Promise.resolve(value ?? operation());
  }

  operation(): Promise<Operation> {
    this.calls.push('operation');
    const seen = this.calls.filter((c) => c === 'operation').length;
    const list = this.options.observations ?? [operation()];
    return Promise.resolve(list[Math.min(seen - 1, list.length - 1)]!);
  }

  ledgerSince(): Promise<{ records: unknown[] }> {
    this.calls.push('ledgerSince');
    return Promise.resolve({ records: [] });
  }

  count(name: string): number {
    return this.calls.filter((c) => c.startsWith(name)).length;
  }
}

export const unreachable = () => new BrokerUnreachable('/fleet', new Error('ECONNREFUSED'));
export const refused = (status: number, detail: string) => new OperationRefused(status, detail);
