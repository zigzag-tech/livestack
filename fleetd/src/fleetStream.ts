/**
 * Authenticated, bounded observation of the Livestack fleet stream.
 *
 * The Hub subscription is account-bound by the Benchday resident's device
 * credentials. This module checks that identity against an explicit account
 * before it opens the subscription. It never reads the broker's `/fleet`
 * route; planning remains on the broker's `/fleet/plan` route.
 */
import { execFile, spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';

import type { FleetView } from './client.js';

export const FLEET_STREAM_CONTRACT = 'harmony.fleet/1';
export const FLEET_STREAM_SUB = 'livestack-fleetd';
export const FLEET_STREAM_TTL_MS = 20_000;
export const MAX_FLEET_PARTIALS = 256;
export const MAX_FLEET_PARTIAL_BYTES = 4_096;
/** One complete cut and one staged replacement are each capped at 1 MiB. */
export const MAX_FLEET_PARTIAL_SET_BYTES = 1_048_576;
export const MAX_FLEET_HELD_PARTIALS = MAX_FLEET_PARTIALS * 2;
export const MAX_FLEET_HELD_BYTES = MAX_FLEET_PARTIAL_SET_BYTES * 2;
export const MAX_FLEET_EVENT_LINE_BYTES = 34_000;
const DEFAULT_RENEW_MS = 10_000;
const CLIENT_COMMAND_TIMEOUT_MS = 5_000;
const CLIENT_COMMAND_OUTPUT_BYTES = 64 * 1024;

export type FleetObservationStatus = 'fresh' | 'unknown' | 'stale' | 'degraded' | 'gap';

export interface FleetObservation {
  status: FleetObservationStatus;
  reason?: string;
  view?: FleetView;
  coverage?: FleetCoverage;
}

export interface FleetObservationSource {
  read(): Promise<FleetObservation>;
}

export interface FleetCoverage {
  expected: string[];
  reporting: string[];
  partial: string[];
  stale: Array<{ source: string; age_ms: number }>;
  failed: Array<{ source: string; code: string }>;
  denied: string[];
  incapable: Array<{ source: string; reason: string }>;
  unreachable: string[];
  complete: boolean;
}

export interface FleetStreamEvent {
  kind: string;
  sub?: string;
  partial?: unknown;
  receipt?: unknown;
  coverage?: unknown;
  state?: unknown;
  reason?: unknown;
  error?: unknown;
}

export interface FleetStreamClientPort {
  startResident(): Promise<void>;
  whoami(): Promise<{ account_id?: unknown }>;
  followEvents(onEvent: (event: FleetStreamEvent) => void, onFailure: (error: Error) => void): {
    stop(): Promise<void>;
  };
  subscribe(): Promise<void>;
  renew(): Promise<void>;
  unsubscribe(): Promise<void>;
  stopResident(): Promise<void>;
}

export interface FleetStreamSourceOptions {
  /** Required account identity expected from `benchday client whoami`. */
  expectedAccountId: string;
  client: FleetStreamClientPort;
  now?: () => number;
  renewEveryMs?: number;
  log?: (message: string) => void;
}

interface HeldPartial {
  source: string;
  subjectMachine: string;
  hostId: string;
  body: Record<string, unknown>;
  receivedAtMs: number;
  receiptAgeMs: number;
  warm: boolean;
  ttlMs: number;
  bytes: number;
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);
}

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > MAX_FLEET_PARTIALS) return null;
  const values = value as unknown[];
  if (!values.every(nonEmptyString) || new Set(values).size !== values.length) return null;
  return values as string[];
}

function coverageFailure(value: unknown): { status: FleetObservationStatus; reason: string } | null {
  const row = object(value);
  if (!row) return { status: 'gap', reason: 'coverage is not an object' };
  const expected = stringList(row.expected);
  const reporting = stringList(row.reporting);
  if (!expected || !reporting) return { status: 'gap', reason: 'coverage source list is malformed or exceeds 256 sources' };
  for (const field of ['partial', 'stale', 'failed', 'denied', 'incapable', 'unreachable']) {
    if (!Array.isArray(row[field]) || (row[field] as unknown[]).length > MAX_FLEET_PARTIALS) {
      return { status: 'gap', reason: `coverage ${field} list is malformed or exceeds 256 rows` };
    }
  }
  if (expected.length === 0) return { status: 'gap', reason: 'coverage names no expected sources' };

  const failed = Array.isArray(row.failed) ? row.failed : [];
  const denied = Array.isArray(row.denied) ? row.denied : [];
  const incapable = Array.isArray(row.incapable) ? row.incapable : [];
  const partial = Array.isArray(row.partial) ? row.partial : [];
  const stale = Array.isArray(row.stale) ? row.stale : [];
  const unreachable = Array.isArray(row.unreachable) ? row.unreachable : [];
  if (failed.length || denied.length || incapable.length || partial.length) {
    return { status: 'degraded', reason: 'coverage reports failed, denied, incapable, or partial sources' };
  }
  if (stale.length) return { status: 'stale', reason: 'coverage reports stale sources' };
  if (unreachable.length || row.complete !== true ||
      expected.length !== reporting.length || expected.some((source) => !reporting.includes(source))) {
    return { status: 'gap', reason: 'coverage is incomplete or an expected source is not reporting' };
  }
  if (expected.some((source) => !reporting.includes(source))) {
    return { status: 'gap', reason: 'coverage reporting set does not cover every expected source' };
  }
  return null;
}

/**
 * Stateful reducer for the resident's ordered `stream.partial`/`stream.coverage`
 * event pair. Kept independently testable so tick integration can exercise
 * source outcomes without connecting to a live Hub.
 */
export class FleetStreamAccumulator implements FleetObservationSource {
  private readonly partials = new Map<string, HeldPartial>();
  private readonly pending = new Map<string, HeldPartial>();
  private readonly now: () => number;
  private accountStatus: 'unknown' | 'matched' | 'degraded' = 'unknown';
  private issue: { status: 'stale' | 'degraded' | 'gap'; reason: string } | null = null;
  private latestCoverage: FleetCoverage | undefined;

  constructor(expectedAccountId: string, now: () => number = Date.now) {
    if (!nonEmptyString(expectedAccountId)) throw new Error('fleet_stream_expected_account_required');
    this.expectedAccountId = expectedAccountId;
    this.now = now;
  }

  private readonly expectedAccountId: string;

  setAuthenticatedAccount(accountId: unknown): void {
    if (typeof accountId !== 'string' || accountId.length === 0) {
      this.accountStatus = 'degraded';
      this.fail('degraded', 'authenticated resident has no account ID');
      return;
    }
    if (accountId !== this.expectedAccountId) {
      this.accountStatus = 'degraded';
      this.fail('degraded', 'authenticated resident account does not match the configured account');
      return;
    }
    this.accountStatus = 'matched';
    this.issue = null;
  }

  markClientFailure(reason: string): void {
    if (this.accountStatus === 'unknown') this.accountStatus = 'degraded';
    this.fail('degraded', `resident client failure: ${reason}`);
  }

  private fail(status: 'degraded' | 'gap', reason: string): void {
    this.pending.clear();
    this.issue = { status, reason: reason.slice(0, 240) };
  }

  private setIssue(status: 'stale' | 'degraded' | 'gap', reason: string): void {
    this.pending.clear();
    this.issue = { status, reason: reason.slice(0, 240) };
  }

  accept(event: FleetStreamEvent): 'ignore' | 'renew' | 'accepted' {
    if (event.sub !== FLEET_STREAM_SUB) return 'ignore';
    if (this.accountStatus !== 'matched') return 'ignore';

    switch (event.kind) {
      case 'stream.status': {
        if (event.state === 'refused' || event.state === 'degraded') {
          this.fail('degraded', `Hub stream state ${String(event.state)}${typeof event.reason === 'string' ? `: ${event.reason}` : ''}`);
        }
        return 'accepted';
      }
      case 'stream.failed':
        this.fail('degraded', `resident stream failure${typeof event.error === 'string' ? `: ${event.error}` : ''}`);
        return 'accepted';
      case 'stream.reset':
        this.partials.clear();
        this.latestCoverage = undefined;
        this.fail('gap', `stream reset${typeof event.reason === 'string' ? `: ${event.reason}` : ''}`);
        return 'renew';
      case 'stream.partial':
        return this.stage(event.partial, event.receipt);
      case 'stream.coverage':
        return this.commit(event.coverage);
      default:
        return 'ignore';
    }
  }

  private stage(rawPartial: unknown, rawReceipt: unknown): 'accepted' {
    const partial = object(rawPartial);
    const receipt = object(rawReceipt);
    if (!partial || partial.contract !== FLEET_STREAM_CONTRACT || !nonEmptyString(partial.source)) {
      this.fail('gap', 'stream partial has an invalid contract or source');
      return 'accepted';
    }
    const subject = object(partial.subject);
    const subjectMachine = subject?.machine;
    const status = partial.status;
    if (status !== 'ok') {
      const error = object(partial.error);
      const code = typeof error?.code === 'string' ? error.code : String(status ?? 'unknown');
      this.fail('degraded', `source ${partial.source} reports ${code}`);
      return 'accepted';
    }
    const body = object(partial.body);
    const hostId = body?.host_id;
    const ageMs = receipt?.age_ms;
    const ttlMs = partial.ttl_ms;
    if (!nonEmptyString(subjectMachine) || !body || !nonEmptyString(hostId) ||
        typeof ageMs !== 'number' || !Number.isSafeInteger(ageMs) || ageMs < 0 ||
        typeof ttlMs !== 'number' || !Number.isSafeInteger(ttlMs) || ttlMs <= 0 || ttlMs > FLEET_STREAM_TTL_MS) {
      this.fail('gap', 'stream partial lacks a valid machine, host, receipt age, or contract TTL');
      return 'accepted';
    }
    const bytes = Buffer.byteLength(JSON.stringify(partial), 'utf8');
    if (bytes > MAX_FLEET_PARTIAL_BYTES) {
      this.fail('gap', `stream partial exceeds ${MAX_FLEET_PARTIAL_BYTES} serialized bytes`);
      return 'accepted';
    }
    const key = `${partial.source}\0${subjectMachine}`;
    if (this.pending.has(key)) {
      this.fail('gap', 'duplicate partial key in one coverage cut');
      return 'accepted';
    }
    const pendingCount = this.pending.size + 1;
    const committedBytes = [...this.partials.values()].reduce((total, held) => total + held.bytes, 0);
    const pendingBytes = [...this.pending.values()].reduce((total, held) => total + held.bytes, 0) + bytes;
    if (this.partials.size > MAX_FLEET_PARTIALS || pendingCount > MAX_FLEET_PARTIALS ||
        this.partials.size + pendingCount > MAX_FLEET_HELD_PARTIALS ||
        committedBytes > MAX_FLEET_PARTIAL_SET_BYTES || pendingBytes > MAX_FLEET_PARTIAL_SET_BYTES ||
        committedBytes + pendingBytes > MAX_FLEET_HELD_BYTES) {
      this.fail('gap', 'stream partial count or committed-plus-pending byte limit exceeded');
      return 'accepted';
    }
    this.pending.set(key, {
      source: partial.source,
      subjectMachine,
      hostId,
      body,
      receivedAtMs: this.now(),
      receiptAgeMs: ageMs,
      // Missing receipt provenance is warm by contract and cannot authorize planning.
      warm: receipt?.warm !== false,
      ttlMs,
      bytes,
    });
    return 'accepted';
  }

  private commit(rawCoverage: unknown): 'accepted' {
    if (this.issue && this.pending.size === 0) return 'accepted';
    if (this.pending.size === 0 && this.partials.size === 0) {
      const failure = coverageFailure(rawCoverage);
      if (failure) {
        this.issue = failure.status === 'unknown' ? null : failure as { status: 'stale' | 'degraded' | 'gap'; reason: string };
        return 'accepted';
      }
      this.issue = null;
      return 'accepted';
    }
    const failure = coverageFailure(rawCoverage);
    if (failure) {
      const status = failure.status === 'stale' || failure.status === 'degraded' || failure.status === 'gap'
        ? failure.status
        : 'gap';
      this.setIssue(status, failure.reason);
      return 'accepted';
    }
    const coverage = object(rawCoverage)! as unknown as FleetCoverage;
    const expected = new Set(coverage.expected);
    const candidate = new Map(this.partials);
    for (const [key, partial] of this.pending) {
      if (!expected.has(partial.source)) {
        this.fail('gap', `partial source ${partial.source} is absent from coverage`);
        return 'accepted';
      }
      candidate.set(key, partial);
    }
    for (const [key, partial] of candidate) {
      if (!expected.has(partial.source)) candidate.delete(key);
    }
    if (candidate.size === 0 || [...expected].some((source) => ![...candidate.values()].some((partial) => partial.source === source))) {
      this.fail('gap', 'complete coverage has no retained partial for every reporting source');
      return 'accepted';
    }
    const hosts = new Set<string>();
    const now = this.now();
    let staleAgeMs = 0;
    for (const partial of candidate.values()) {
      if (hosts.has(partial.hostId)) {
        this.fail('gap', `multiple stream sources claim host ${partial.hostId}`);
        return 'accepted';
      }
      hosts.add(partial.hostId);
      const ageMs = partial.receiptAgeMs + Math.max(0, now - partial.receivedAtMs);
      staleAgeMs = Math.max(staleAgeMs, ageMs);
      if (ageMs > Math.min(FLEET_STREAM_TTL_MS, partial.ttlMs)) {
        this.pending.clear();
        this.partials.clear();
        this.issue = { status: 'stale', reason: `source ${partial.source} receipt age ${Math.ceil(ageMs)}ms exceeds its TTL` };
        this.latestCoverage = coverage;
        return 'accepted';
      }
    }
    if (staleAgeMs > FLEET_STREAM_TTL_MS) {
      this.fail('gap', 'stream coverage is older than the contract TTL');
      return 'accepted';
    }
    this.partials.clear();
    for (const [key, partial] of candidate) this.partials.set(key, partial);
    this.pending.clear();
    this.latestCoverage = coverage;
    this.issue = null;
    return 'accepted';
  }

  async read(): Promise<FleetObservation> {
    if (this.accountStatus === 'degraded') {
      return { status: 'degraded', reason: this.issue?.reason ?? 'resident authentication is unavailable' };
    }
    if (this.accountStatus !== 'matched') return { status: 'unknown', reason: 'waiting for authenticated resident identity' };
    if (this.issue) return { ...this.issue };
    if (!this.latestCoverage || this.partials.size === 0) {
      return { status: 'unknown', reason: 'waiting for a complete harmony.fleet/1 sample' };
    }
    const now = this.now();
    const hosts = Object.create(null) as FleetView['hosts'];
    let oldestAgeMs = 0;
    let warmSource: string | undefined;
    for (const partial of this.partials.values()) {
      const ageMs = partial.receiptAgeMs + Math.max(0, now - partial.receivedAtMs);
      oldestAgeMs = Math.max(oldestAgeMs, ageMs);
      if (ageMs > Math.min(FLEET_STREAM_TTL_MS, partial.ttlMs)) {
        return { status: 'stale', reason: `source ${partial.source} receipt age ${Math.ceil(ageMs)}ms exceeds its TTL` };
      }
      if (partial.warm && warmSource === undefined) warmSource = partial.source;
      hosts[partial.hostId] = {
        ...partial.body,
        ...(Array.isArray(partial.body.nodes) ? { nodes: partial.body.nodes } : {}),
        stream_source: partial.source,
        stream_subject: partial.subjectMachine,
        stream_age_ms: Math.ceil(ageMs),
      };
    }
    if (warmSource !== undefined) {
      return { status: 'stale', reason: `source ${warmSource} is a warm cached reading awaiting a live sample` };
    }
    if (oldestAgeMs > FLEET_STREAM_TTL_MS) {
      return { status: 'stale', reason: `oldest stream receipt age ${Math.ceil(oldestAgeMs)}ms exceeds ${FLEET_STREAM_TTL_MS}ms` };
    }
    return {
      status: 'fresh',
      view: { generated_at: now / 1000, hosts },
      coverage: this.latestCoverage,
    };
  }
}

export interface FleetStreamSource extends FleetObservationSource {
  start(): Promise<void>;
  stop(): Promise<void>;
}

export function makeBenchdayFleetStreamSource(
  options: FleetStreamSourceOptions,
): FleetStreamSource {
  const accumulator = new FleetStreamAccumulator(options.expectedAccountId, options.now);
  const log = options.log ?? (() => {});
  let started = false;
  let subscribed = false;
  let follower: { stop(): Promise<void> } | null = null;
  let renewTimer: ReturnType<typeof setInterval> | null = null;
  const renewEveryMs = options.renewEveryMs ?? DEFAULT_RENEW_MS;

  const onFailure = (error: Error) => {
    accumulator.markClientFailure(error.message);
    log(`[fleetd] harmony.fleet/1 event transport failed: ${error.message.slice(0, 200)}`);
  };

  return {
    async start() {
      if (started) return;
      started = true;
      try {
        await options.client.startResident();
        const identity = await options.client.whoami();
        accumulator.setAuthenticatedAccount(identity.account_id);
        const identityRead = await accumulator.read();
        if (identityRead.status === 'degraded') {
          log(`[fleetd] harmony.fleet/1 ${identityRead.status}: ${identityRead.reason}`);
          return;
        }
        follower = options.client.followEvents((event) => {
          const accepted = accumulator.accept(event);
          if (accepted === 'renew') {
            void options.client.renew().catch(onFailure);
          }
        }, onFailure);
        await options.client.subscribe();
        subscribed = true;
        renewTimer = setInterval(() => {
          if (subscribed) void options.client.renew().catch(onFailure);
        }, renewEveryMs);
        renewTimer.unref?.();
      } catch (error) {
        onFailure(error instanceof Error ? error : new Error(String(error)));
      }
    },
    read: () => accumulator.read(),
    async stop() {
      if (renewTimer) clearInterval(renewTimer);
      renewTimer = null;
      const errors: string[] = [];
      if (subscribed) {
        try { await options.client.unsubscribe(); } catch (error) { errors.push(String(error)); }
      }
      subscribed = false;
      if (follower) {
        try { await follower.stop(); } catch (error) { errors.push(String(error)); }
      }
      follower = null;
      if (started) {
        try { await options.client.stopResident(); } catch (error) { errors.push(String(error)); }
      }
      started = false;
      if (errors.length) throw new Error(`fleet_stream_stop_failed: ${errors.join('; ').slice(0, 300)}`);
    },
  };
}

export interface BenchdayClientPortOptions {
  executable?: string;
  instance?: string;
  log?: (message: string) => void;
}

export interface ConfiguredFleetStreamOptions {
  now?: () => number;
  renewEveryMs?: number;
  log?: (message: string) => void;
}

export function benchdayFleetStreamCommand(
  action: 'subscribe' | 'renew' | 'unsubscribe',
  instance: string,
): string[] {
  return [
    '--json', 'client', 'streams', action,
    '--route', 'hub', '--scope', 'account', '--sub', FLEET_STREAM_SUB,
    '--contract', FLEET_STREAM_CONTRACT, '--instance', instance,
  ];
}

/** Build the production source from explicit account configuration and the installed CLI. */
export function makeConfiguredFleetStreamSource(
  options: ConfiguredFleetStreamOptions = {},
): FleetStreamSource {
  const expectedAccountId = process.env.FLEETD_STREAM_ACCOUNT_ID;
  if (!expectedAccountId) throw new Error('FLEETD_STREAM_ACCOUNT_ID is required for authenticated fleet observation');
  return makeBenchdayFleetStreamSource({
    expectedAccountId,
    client: benchdayClientPort({ log: options.log }),
    now: options.now,
    renewEveryMs: options.renewEveryMs,
    log: options.log,
  });
}

/** Process adapter for the installed Benchday resident CLI. Arguments contain no credentials. */
export function benchdayClientPort(options: BenchdayClientPortOptions = {}): FleetStreamClientPort {
  const executable = options.executable ?? process.env.FLEETD_BENCHDAY_BIN ?? 'benchday';
  const instance = options.instance ?? process.env.FLEETD_BENCHDAY_INSTANCE ?? FLEET_STREAM_SUB;
  const log = options.log ?? (() => {});
  let eventChild: ChildProcess | null = null;
  let eventClosed = false;

  const run = async (args: string[]): Promise<Record<string, unknown>> => {
    const argv = ['--json', ...args];
    const result = await new Promise<{ stdout: string; stderr: string }>((resolve, reject) => {
      execFile(executable, argv, {
        timeout: CLIENT_COMMAND_TIMEOUT_MS,
        maxBuffer: CLIENT_COMMAND_OUTPUT_BYTES,
        encoding: 'utf8',
      }, (error, stdout, stderr) => {
        if (error) {
          reject(new Error(`benchday ${args.slice(1).join(' ')} failed: ${(stderr || error.message).slice(0, 240)}`));
          return;
        }
        resolve({ stdout, stderr });
      });
    });
    let value: unknown;
    try { value = JSON.parse(result.stdout); } catch {
      throw new Error(`benchday ${args.slice(1).join(' ')} returned invalid JSON`);
    }
    const row = object(value);
    if (!row || row.ok !== true) {
      throw new Error(`benchday ${args.slice(1).join(' ')} returned no successful result`);
    }
    return row;
  };

  const streamCommand = (action: 'subscribe' | 'renew' | 'unsubscribe') => run(
    benchdayFleetStreamCommand(action, instance).slice(1),
  ).then((result) => {
    if (result.sent !== true) throw new Error(`benchday stream ${action} was not sent`);
  });

  return {
    startResident: async () => {
      await run(['client', 'start', '--name', 'Livestack FleetD', '--instance', instance]);
    },
    whoami: async () => run(['client', 'whoami', '--instance', instance]),
    followEvents: (onEvent, onFailure) => {
      if (eventChild) throw new Error('fleet_stream_event_follower_already_running');
      eventClosed = false;
      const child = spawn(executable, ['client', 'events', '--follow', '--instance', instance], {
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      if (!child.stdout || !child.stderr) throw new Error('benchday event follower did not provide output pipes');
      eventChild = child;
      let buffered = Buffer.alloc(0);
      let discardUntilNewline = false;
      const emitLine = (line: Buffer) => {
        if (line.length === 0) return;
        let event: FleetStreamEvent;
        try { event = JSON.parse(line.toString('utf8')) as FleetStreamEvent; }
        catch {
          onFailure(new Error('benchday event stream emitted invalid JSON'));
          return;
        }
        if (!object(event) || typeof event.kind !== 'string') {
          onFailure(new Error('benchday event stream emitted an invalid event'));
          return;
        }
        onEvent(event);
      };
      child.stdout.on('data', (chunk: Buffer) => {
        let offset = 0;
        while (offset < chunk.length) {
          const newline = chunk.indexOf(0x0a, offset);
          const end = newline < 0 ? chunk.length : newline;
          const fragment = chunk.subarray(offset, end);
          if (!discardUntilNewline) {
            if (buffered.length + fragment.length > MAX_FLEET_EVENT_LINE_BYTES) {
              buffered = Buffer.alloc(0);
              onFailure(new Error(`benchday event line exceeds ${MAX_FLEET_EVENT_LINE_BYTES} bytes`));
              discardUntilNewline = newline < 0;
            } else {
              buffered = buffered.length === 0 ? Buffer.from(fragment) : Buffer.concat([buffered, fragment]);
            }
          }
          if (newline >= 0) {
            if (!discardUntilNewline) emitLine(buffered);
            buffered = Buffer.alloc(0);
            discardUntilNewline = false;
            offset = newline + 1;
          } else {
            offset = chunk.length;
          }
        }
      });
      child.stderr.on('data', (chunk: Buffer) => {
        log(`[fleetd] benchday event follower: ${chunk.toString('utf8').slice(-240)}`);
      });
      child.once('error', (error) => { if (!eventClosed) onFailure(error); });
      child.once('exit', (code, signal) => {
        eventChild = null;
        if (!eventClosed) onFailure(new Error(`benchday event follower exited (${code ?? signal ?? 'unknown'})`));
      });
      return {
        stop: async () => {
          if (eventChild !== child) return;
          eventClosed = true;
          child.kill('SIGTERM');
          await new Promise<void>((resolve) => {
            const timeout = setTimeout(() => {
              child.kill('SIGKILL');
              resolve();
            }, 2_000);
            timeout.unref?.();
            child.once('exit', () => { clearTimeout(timeout); resolve(); });
          });
          eventChild = null;
        },
      };
    },
    subscribe: () => streamCommand('subscribe'),
    renew: () => streamCommand('renew'),
    unsubscribe: () => streamCommand('unsubscribe'),
    stopResident: async () => {
      await run(['client', 'stop', '--instance', instance]);
    },
  };
}
