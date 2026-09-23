/**
 * The broker's control plane, typed.
 *
 * Three routes with a strict division of labour, mirrored here so a reader of
 * the loop can see which calls cost money: `plan` READS, `startOperation`
 * SPENDS, `operation` OBSERVES. Everything else is a read.
 *
 * The one rule this file enforces on its own: **a refusal is a value, not an
 * exception to be flattened.** `startOperation` throws a typed
 * `OperationRefused` carrying the broker's status and its sentence, because
 * "409: this owner is at its ceiling" and "the network is down" want opposite
 * responses and a client that returns `null` for both makes them the same.
 */

export interface StructuredError {
  stage: string;
  class: string;
  code: string;
  excerpt: string;
}

export type OperationState =
  | 'intent'
  | 'creating'
  | 'created'
  | 'rejected'
  | 'uncertain'
  | 'announced'
  | 'failed'
  | 'released';

export interface Operation {
  operation_id: string;
  idempotency_key: string;
  job_id: string;
  kind: string;
  owner: string;
  target_id: string;
  state: OperationState;
  created_at: number;
  updated_at: number;
  terminal: boolean;
  provider?: string | null;
  region?: string | null;
  plan_version?: string | null;
  announce_deadline?: number | null;
  provider_instance_id?: string | null;
  node_id?: string | null;
  error?: StructuredError | null;
  reason?: string | null;
  observability_degraded: boolean;
}

export type PlanAction =
  | { type: 'admit'; job_id: string; target_id: string; est_cost: number; reason: string }
  | {
      type: 'provision';
      job_id: string;
      target_id: string;
      tier: string;
      est_cost: number;
      reason: string;
    }
  | { type: 'queue'; job_id: string; reason: string }
  | { type: 'deprovision'; target_id: string; reason: string };

/** The two actions this loop ACTS on. `admit` and `queue` need no operation. */
export type ActingAction = Extract<PlanAction, { type: 'provision' | 'deprovision' }>;

export interface FleetPlan {
  api: string;
  plan_version: string;
  generated_at: number;
  jobs: Array<{ job_id: string; kind: string; owner: string; sla: string }>;
  pools: Array<{ target_id: string; tier: string; provider?: string; region?: string }>;
  excluded: Array<{ target_id?: string; pool_id?: string; reason: string }>;
  /** Inputs the fleet did NOT know. Never empty because nothing was uncertain — empty
   * because nothing was assumed. The distinction is the gate's business. */
  uncertainty: Array<{ target_id: string; field: string; assumed: number; reason: string }>;
  reservations: Array<{ operation_id: string; job_id: string; target_id: string; state: string }>;
  policy: { digest: string; weights: Record<string, number> };
  actions: PlanAction[];
}

export interface FleetNode {
  peer: string;
  state: string;
  ready?: boolean;
  unseen_seconds: number;
  operation_id?: string | null;
  kinds: string[];
  load?: { in_flight?: number } | null;
}

export interface FleetView {
  generated_at: number;
  hosts: Record<string, { nodes?: FleetNode[] }>;
  pools?: unknown[];
  operations?: { active: Operation[]; observability_degraded: string[] };
}

export interface JobRequest {
  job_id: string;
  kind: string;
  sla?: 'interactive' | 'normal' | 'batch';
  created_at?: number;
  deadline_at?: number;
  est_duration_s?: number;
  selector?: Record<string, string>;
  locality_host?: string;
}

/** A refusal the broker STATED. Carries its status and its sentence. */
export class OperationRefused extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
    this.name = 'OperationRefused';
  }
}

/** The transport failed. NOT the same as a refusal, and never flattened into one. */
export class BrokerUnreachable extends Error {
  constructor(
    readonly route: string,
    readonly cause: unknown,
  ) {
    super(`${route}: ${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'BrokerUnreachable';
  }
}

export interface FleetClient {
  view(): Promise<FleetView>;
  plan(input: { jobs: JobRequest[]; regions?: string }): Promise<FleetPlan>;
  startOperation(input: {
    action: ActingAction;
    planVersion: string;
    idempotencyKey?: string;
    regions?: string;
  }): Promise<Operation>;
  operation(id: string): Promise<Operation>;
  ledgerSince(since: number, limit?: number): Promise<{ records: unknown[] }>;
}

export interface HttpClientOptions {
  baseUrl: string;
  /** Bearer token, when the broker has principals configured. */
  token?: string;
  /** Owner to act as, for a delegating principal. */
  owner?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export function httpFleetClient(options: HttpClientOptions): FleetClient {
  const base = options.baseUrl.replace(/\/+$/, '');
  const doFetch = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? 20_000;

  async function call(route: string, init?: RequestInit & { body?: string }) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await doFetch(`${base}${route}`, {
        ...init,
        signal: controller.signal,
        headers: {
          'content-type': 'application/json',
          ...(options.token ? { authorization: `Bearer ${options.token}` } : {}),
          ...(init?.headers ?? {}),
        },
      });
    } catch (cause) {
      // A transport failure is not a refusal. Conflating them is how a loop
      // decides an owner is over quota because a cable was unplugged.
      throw new BrokerUnreachable(route, cause);
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    const parsed = text.trim() ? safeJson(text) : {};
    if (!response.ok) {
      const detail =
        parsed && typeof parsed === 'object' && 'detail' in parsed
          ? String((parsed as { detail: unknown }).detail)
          : text.slice(0, 300);
      throw new OperationRefused(response.status, detail);
    }
    return parsed;
  }

  return {
    view: () => call('/fleet') as Promise<FleetView>,
    plan: (input) =>
      call('/fleet/plan', {
        method: 'POST',
        body: JSON.stringify({
          jobs: input.jobs,
          ...(input.regions ? { regions: input.regions } : {}),
          ...(options.owner ? { owner: options.owner } : {}),
        }),
      }) as Promise<FleetPlan>,
    startOperation: (input) =>
      call('/fleet/operations', {
        method: 'POST',
        body: JSON.stringify({
          action: input.action,
          plan_version: input.planVersion,
          ...(input.idempotencyKey ? { idempotency_key: input.idempotencyKey } : {}),
          ...(input.regions ? { regions: input.regions } : {}),
          ...(options.owner ? { owner: options.owner } : {}),
        }),
      }) as Promise<Operation>,
    operation: (id) => call(`/fleet/operations/${encodeURIComponent(id)}`) as Promise<Operation>,
    ledgerSince: (since, limit = 200) =>
      call(`/fleet/ledger?since=${since}&limit=${limit}`) as Promise<{ records: unknown[] }>,
  };
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text.slice(0, 300) };
  }
}

/**
 * The idempotency key for one attempt at one action.
 *
 * It must be STABLE for the same intent and DIFFERENT for a genuinely new one —
 * which is why it is derived from the job and the target rather than from a
 * clock or a random number. A key the loop cannot reproduce after a restart is
 * the same as having no key at all the first time a reply is lost.
 */
export function idempotencyKey(action: ActingAction, attempt = 1): string {
  const job = 'job_id' in action ? action.job_id : 'none';
  return `${action.type}:${job}:${action.target_id}:${attempt}`;
}
