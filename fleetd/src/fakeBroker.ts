/**
 * A fake control plane that misbehaves the way the real one does.
 *
 * It counts calls, because the properties worth asserting here are mostly about
 * what did NOT happen: no plan call after an unreadable view, no operation call
 * after a malformed plan, no escalation on the green path, no model on a
 * failure the table already knows.
 */
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

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
import type {
  BrokerPolicyStatus,
  PolicyBroker,
  PolicyPutResult,
  PolicyRevertResult,
} from './policy/broker.js';

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

// ---------------------------------------------------------------------------
// The broker's policy routes (design §6), for the policy improver's tests.
// ---------------------------------------------------------------------------

interface FakeArtifact {
  version: string;
  [key: string]: unknown;
}

/**
 * The broker's policy files as hostd keeps them — active, previous, shadow — written to a
 * real policy directory, because the improver's bootstrap reads `<id>.active.json` from it.
 * It does not validate (the real route validates natively); it counts, so tests can assert
 * what was NOT published.
 */
export class FakePolicyBroker implements PolicyBroker {
  readonly calls: string[] = [];
  active: FakeArtifact | null = null;
  previous: FakeArtifact | null = null;
  shadow: FakeArtifact[] = [];
  /** Reported version override, to simulate a broker deciding with something else. */
  reportActive: string | null = null;
  failPut: Error | null = null;

  constructor(readonly policyDir: string, readonly policyId: string) {
    mkdirSync(policyDir, { recursive: true });
  }

  /** Put an artifact in place as a person would have (task 6.3), without counting a call. */
  seed(artifact: FakeArtifact): void {
    this.active = artifact;
    this.write();
  }

  async status(policyId: string): Promise<BrokerPolicyStatus> {
    this.calls.push(`status:${policyId}`);
    const version = this.reportActive ?? this.active?.version ?? 'b3:defaults';
    return {
      policy_id: policyId,
      source: this.active || this.reportActive ? 'file' : 'defaults',
      active: { version },
      previous: { version: this.previous?.version ?? null },
      shadow: this.shadow.map((s) => ({ version: s.version })),
      degraded: this.active ? [] : ['policy_artifact_missing'],
    };
  }

  async put(policyId: string, role: 'active' | 'shadow', body: unknown): Promise<PolicyPutResult> {
    this.calls.push(`put:${role}`);
    if (this.failPut) throw this.failPut;
    const previous = this.active?.version ?? null;
    if (role === 'active') {
      if (this.active) this.previous = this.active;
      this.active = body as FakeArtifact;
    } else {
      this.shadow = body as FakeArtifact[];
    }
    this.write();
    return {
      policy_id: policyId,
      role,
      version: role === 'active' ? (body as FakeArtifact).version : (body as FakeArtifact[]).map((a) => a.version),
      previous_version: role === 'active' ? previous : null,
    };
  }

  async revert(policyId: string): Promise<PolicyRevertResult> {
    this.calls.push('revert');
    if (!this.previous) throw new OperationRefused(409, 'no previous artifact to revert to');
    [this.active, this.previous] = [this.previous, this.active];
    this.write();
    return { policy_id: policyId, version: this.active!.version, previous_version: this.previous?.version ?? null };
  }

  private write(): void {
    const file = (role: string) => join(this.policyDir, `${this.policyId}.${role}.json`);
    for (const [role, value] of [['active', this.active], ['previous', this.previous]] as const) {
      if (value) writeFileSync(file(role), JSON.stringify(value));
      else rmSync(file(role), { force: true });
    }
  }
}
