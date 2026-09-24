/**
 * The broker's policy routes (design §6), typed, and the two `PolicyPublisher`s the
 * improver hands to jingway's activation service.
 *
 * `GET /fleet/policy/{id}` is a read and needs no token. `PUT` and `POST …/revert` change
 * routing for everyone: they carry the `policy_admin` bearer token read from
 * `$LIVESTACK_POLICY_ADMIN_TOKEN_FILE` (a file, never an environment value — the fleet
 * token-file procedure). Failures keep client.ts's split: a refusal the broker STATED is an
 * `OperationRefused` with its status, a transport failure is `BrokerUnreachable`.
 */
import { readFile } from 'node:fs/promises';

import type { PolicyPublisher } from 'jingway-framework/server/routines/RoutineActivationService.js';
import type { PolicyArtifact } from 'jingway-framework/server/policy';

import { BrokerUnreachable, OperationRefused } from '../client.js';

/** `PolicyRuntime.status()` (node-py/livestack_node/policy_runtime.py), the fields read here. */
export interface BrokerPolicyStatus {
  policy_id: string;
  /** `file` when an active artifact file is loaded; `defaults` when the broker runs on compiled defaults. */
  source: 'file' | 'defaults';
  active: { version: string; provenance?: unknown; loaded_at?: number; version_verified?: boolean };
  previous: { version: string | null };
  shadow: Array<{ version: string }>;
  native?: boolean;
  mode?: string;
  mismatches?: number;
  last_load_error?: string | null;
  degraded?: string[];
}

export interface PolicyPutResult {
  policy_id: string;
  role: 'active' | 'shadow';
  version: string | string[];
  previous_version: string | null;
}

export interface PolicyRevertResult {
  policy_id: string;
  version: string;
  previous_version: string | null;
}

export interface PolicyBroker {
  status(policyId: string): Promise<BrokerPolicyStatus>;
  put(policyId: string, role: 'active' | 'shadow', body: unknown): Promise<PolicyPutResult>;
  revert(policyId: string): Promise<PolicyRevertResult>;
}

export interface HttpPolicyBrokerOptions {
  baseUrl: string;
  /** The `policy_admin` bearer token. Required for `put`/`revert`, unused by `status`. */
  token?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export function httpPolicyBroker(options: HttpPolicyBrokerOptions): PolicyBroker {
  const base = options.baseUrl.replace(/\/+$/, '');
  const doFetch = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? 20_000;

  async function call(route: string, init: { method?: string; body?: unknown; admin?: boolean } = {}): Promise<unknown> {
    if (init.admin && !options.token) {
      // Refuse here rather than send an unauthenticated write the broker would 401: the
      // message that helps is "you gave me no token", not the broker's reply to its absence.
      throw new Error(`${route} needs the policy_admin token (set LIVESTACK_POLICY_ADMIN_TOKEN_FILE)`);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await doFetch(`${base}${route}`, {
        method: init.method ?? 'GET',
        signal: controller.signal,
        headers: {
          'content-type': 'application/json',
          ...(init.admin ? { authorization: `Bearer ${options.token}` } : {}),
        },
        ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
      });
    } catch (cause) {
      throw new BrokerUnreachable(route, cause);
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    let parsed: unknown = {};
    if (text.trim()) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { detail: text.slice(0, 300) };
      }
    }
    if (!response.ok) {
      const detail = parsed && typeof parsed === 'object' && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : text.slice(0, 300);
      // A 422's detail is the full violation list; keep it whole, it is the useful part.
      throw new OperationRefused(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return parsed;
  }

  const route = (policyId: string) => `/fleet/policy/${encodeURIComponent(policyId)}`;
  return {
    status: (policyId) => call(route(policyId)) as Promise<BrokerPolicyStatus>,
    put: (policyId, role, body) =>
      call(`${route(policyId)}?role=${role}`, { method: 'PUT', body, admin: true }) as Promise<PolicyPutResult>,
    revert: (policyId) => call(`${route(policyId)}/revert`, { method: 'POST', admin: true }) as Promise<PolicyRevertResult>,
  };
}

/** The `policy_admin` token from `$LIVESTACK_POLICY_ADMIN_TOKEN_FILE`. Throws, naming the variable. */
export async function readAdminToken(env: NodeJS.ProcessEnv = process.env): Promise<string> {
  const file = env.LIVESTACK_POLICY_ADMIN_TOKEN_FILE;
  if (!file) throw new Error('LIVESTACK_POLICY_ADMIN_TOKEN_FILE is not set; publishing a policy needs the policy_admin token');
  const token = (await readFile(file, 'utf8')).trim();
  if (!token) throw new Error(`LIVESTACK_POLICY_ADMIN_TOKEN_FILE (${file}) is empty`);
  return token;
}

function versionOf(artifactJson: string): string {
  return (JSON.parse(artifactJson) as PolicyArtifact).version;
}

/**
 * Publish = an authenticated `PUT …?role=active`, then check the broker loaded the version
 * the ledger is about to record. The broker recomputes the version natively; a mismatch
 * means the ledger would name an artifact no host decides with, so it throws, and the
 * activation is recorded `blocked`.
 */
export function brokerPublisher(broker: PolicyBroker): PolicyPublisher {
  return {
    async publish(policyId, artifactJson) {
      const expected = versionOf(artifactJson);
      const result = await broker.put(policyId, 'active', JSON.parse(artifactJson));
      if (result.version !== expected) {
        throw new Error(`broker loaded ${String(result.version)} where ${expected} was published`);
      }
    },
  };
}

/**
 * For the bootstrap only: the artifact is ALREADY the broker's active one (a person put it
 * there, task 6.3), so "publishing" it is checking that, not writing it again. A second PUT
 * of the same artifact would copy it over `.previous.json` and destroy the revert target.
 */
export function alreadyPublished(broker: PolicyBroker): PolicyPublisher {
  return {
    async publish(policyId, artifactJson) {
      const expected = versionOf(artifactJson);
      const status = await broker.status(policyId);
      if (status.source !== 'file' || status.active.version !== expected) {
        throw new Error(`the broker is not deciding with ${expected} (source ${status.source}, active ${status.active.version})`);
      }
    },
  };
}

/**
 * For a revert: `POST …/revert` swaps the broker's previous file back in. The caller has
 * already checked that the broker's previous IS the version being activated; this checks
 * the swap landed on it.
 */
export function revertPublisher(broker: PolicyBroker): PolicyPublisher {
  return {
    async publish(policyId, artifactJson) {
      const expected = versionOf(artifactJson);
      const result = await broker.revert(policyId);
      if (result.version !== expected) {
        throw new Error(`revert left the broker on ${result.version}, expected ${expected}`);
      }
    },
  };
}
