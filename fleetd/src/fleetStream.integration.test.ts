import assert from 'node:assert/strict';
import { test } from 'node:test';

import { noRepairHost } from 'jingway-framework/server/wovine/noRepairHost.js';

import { FakeBroker, NOW } from './fakeBroker.js';
import { httpFleetClient } from './client.js';
import {
  benchdayFleetStreamCommand,
  FLEET_STREAM_CONTRACT,
  FLEET_STREAM_SUB,
  FleetStreamEvent,
  FleetStreamClientPort,
  makeBenchdayFleetStreamSource,
  MAX_FLEET_PARTIALS,
  MAX_FLEET_PARTIAL_BYTES,
  MAX_FLEET_HELD_PARTIALS,
  MAX_FLEET_HELD_BYTES,
} from './fleetStream.js';
import { fleetTick } from './tick.js';

class FakeBenchdayClient implements FleetStreamClientPort {
  accountId = 'account-a';
  subscribeCalls = 0;
  renewCalls = 0;
  unsubscribeCalls = 0;
  stopCalls = 0;
  onEvent?: (event: FleetStreamEvent) => void;
  onFailure?: (error: Error) => void;
  failStart = false;
  failWhoami = false;
  failSubscribe = false;

  async startResident(): Promise<void> {
    if (this.failStart) throw new Error('resident_start_failed');
  }
  async whoami(): Promise<{ account_id: unknown }> {
    if (this.failWhoami) throw new Error('resident_identity_unavailable');
    return { account_id: this.accountId };
  }
  followEvents(onEvent: (event: FleetStreamEvent) => void, onFailure: (error: Error) => void) {
    this.onEvent = onEvent;
    this.onFailure = onFailure;
    return { stop: async () => { this.onEvent = undefined; } };
  }
  async subscribe(): Promise<void> {
    this.subscribeCalls += 1;
    if (this.failSubscribe) throw new Error('lease_refused');
  }
  async renew(): Promise<void> { this.renewCalls += 1; }
  async unsubscribe(): Promise<void> { this.unsubscribeCalls += 1; }
  async stopResident(): Promise<void> { this.stopCalls += 1; }
  emit(event: FleetStreamEvent): void {
    assert.ok(this.onEvent, 'event follower must be running before subscribe');
    this.onEvent(event);
  }
}

function source(client = new FakeBenchdayClient()) {
  const observation = makeBenchdayFleetStreamSource({
    expectedAccountId: 'account-a',
    client,
    now: () => NOW * 1000,
    renewEveryMs: 60_000,
  });
  return { client, observation };
}

function partial(options: { source?: string; machine?: string; host?: string; body?: Record<string, unknown> } = {}) {
  const source = options.source ?? 'daemon:host-a';
  return {
    contract: FLEET_STREAM_CONTRACT,
    source,
    subject: { machine: options.machine ?? source.slice('daemon:'.length) },
    epoch: 1,
    seq: 1,
    produced_ms: NOW * 1000,
    ttl_ms: 20_000,
    status: 'ok',
    body: { host_id: options.host ?? 'host-a', node_state: 'up', membership_state: 'fresh', ...(options.body ?? {}) },
  };
}

function coverage(overrides: Record<string, unknown> = {}) {
  return {
    expected: ['daemon:host-a'],
    reporting: ['daemon:host-a'],
    partial: [], stale: [], failed: [], denied: [], incapable: [], unreachable: [],
    complete: true,
    ...overrides,
  };
}

function publishComplete(client: FakeBenchdayClient, p = partial(), c = coverage(), receiptAgeMs = 0, warm = false) {
  client.emit({ kind: 'stream.partial', sub: FLEET_STREAM_SUB, partial: p, receipt: { age_ms: receiptAgeMs, warm } });
  client.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: c });
}

test('authenticated account stream feeds the tick and preserves host facts without inventing worker rows', async () => {
  const { client: streamClient, observation } = source();
  assert.equal((await observation.read()).status, 'unknown');
  await observation.start();
  assert.equal(streamClient.subscribeCalls, 1);
  publishComplete(streamClient, partial({ body: { units: [{ kind: 'llm' }], demand_unmet: 2 } }));

  const sample = await observation.read();
  assert.equal(sample.status, 'fresh');
  assert.deepEqual(sample.view?.hosts['host-a']?.units, [{ kind: 'llm' }]);
  assert.equal('nodes' in (sample.view?.hosts['host-a'] ?? {}), false);

  const broker = new FakeBroker();
  const result = await fleetTick({
    client: broker,
    fleetSource: observation,
    jobs: [],
    now: () => NOW,
    observeHost: noRepairHost('integration test'),
  });
  assert.ok(result.plan);
  assert.equal(broker.count('plan'), 1);
  await observation.stop();
  assert.equal(streamClient.unsubscribeCalls, 1);
  assert.equal(streamClient.stopCalls, 1);
});

test('account mismatch blocks subscription and planning as degraded', async () => {
  const streamClient = new FakeBenchdayClient();
  streamClient.accountId = 'account-b';
  const observation = makeBenchdayFleetStreamSource({ expectedAccountId: 'account-a', client: streamClient });
  await observation.start();
  const broker = new FakeBroker();
  const result = await fleetTick({ client: broker, fleetSource: observation, jobs: [], now: () => NOW });
  assert.equal((await observation.read()).status, 'degraded');
  assert.equal(streamClient.subscribeCalls, 0);
  assert.equal(broker.count('plan'), 0);
  assert.match(result.blocked?.reason ?? '', /harmony\.fleet\/1 degraded/, result.blocked?.reason ?? 'missing blocked result');
  await observation.stop();
});

test('resident and subscription failures surface as degraded observations', async () => {
  for (const failure of ['start', 'whoami', 'subscribe'] as const) {
    const streamClient = new FakeBenchdayClient();
    if (failure === 'start') streamClient.failStart = true;
    if (failure === 'whoami') streamClient.failWhoami = true;
    if (failure === 'subscribe') streamClient.failSubscribe = true;
    const observation = makeBenchdayFleetStreamSource({ expectedAccountId: 'account-a', client: streamClient });
    await observation.start();
    assert.equal((await observation.read()).status, 'degraded', failure);
    const broker = new FakeBroker();
    await fleetTick({ client: broker, fleetSource: observation, jobs: [], now: () => NOW });
    assert.equal(broker.count('plan'), 0, failure);
    await observation.stop();
  }
});

test('unknown, stale, degraded and gap each stop before the planning route', async () => {
  const cases: Array<{ name: string; emit: (client: FakeBenchdayClient) => void; status: string }> = [
    { name: 'unknown', status: 'unknown', emit: () => {} },
    { name: 'stale', status: 'stale', emit: (client) => publishComplete(client, partial(), coverage(), 20_001) },
    { name: 'warm cache', status: 'stale', emit: (client) => publishComplete(client, partial(), coverage(), 0, true) },
    { name: 'missing receipt provenance', status: 'stale', emit: (client) => {
      client.emit({ kind: 'stream.partial', sub: FLEET_STREAM_SUB, partial: partial(), receipt: { age_ms: 0 } });
      client.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage() });
    } },
    { name: 'degraded', status: 'degraded', emit: (client) => client.emit({ kind: 'stream.status', sub: FLEET_STREAM_SUB, state: 'refused', reason: 'not_allowed' }) },
    { name: 'gap', status: 'gap', emit: (client) => client.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage({ reporting: [], unreachable: ['daemon:host-a'], complete: false }) }) },
    { name: 'empty expected source set', status: 'gap', emit: (client) => client.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage({ expected: [], reporting: [], complete: true }) }) },
    { name: 'coverage row bound exceeded', status: 'gap', emit: (client) => client.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage({ failed: Array.from({ length: 257 }, () => ({ source: 'daemon:host-a', code: 'failed' })) }) }) },
  ];
  for (const item of cases) {
    const { client: streamClient, observation } = source();
    await observation.start();
    item.emit(streamClient);
    const broker = new FakeBroker();
    const result = await fleetTick({ client: broker, fleetSource: observation, jobs: [], now: () => NOW });
    assert.equal((await observation.read()).status, item.status, item.name);
    assert.equal(broker.count('plan'), 0, item.name);
    assert.match(result.blocked?.reason ?? '', new RegExp(`harmony\\.fleet/1 ${item.status}`), `${item.name}: ${result.blocked?.reason}`);
    await observation.stop();
  }
});

test('events without the dedicated subscription id cannot seed fleet observations', async () => {
  const { client: streamClient, observation } = source();
  await observation.start();
  streamClient.emit({ kind: 'stream.partial', partial: partial(), receipt: { age_ms: 0, warm: false } });
  streamClient.emit({ kind: 'stream.coverage', coverage: coverage() });
  streamClient.emit({ kind: 'stream.partial', sub: 'another-subscription', partial: partial(), receipt: { age_ms: 0, warm: false } });
  streamClient.emit({ kind: 'stream.coverage', sub: 'another-subscription', coverage: coverage() });
  assert.equal((await observation.read()).status, 'unknown');
  publishComplete(streamClient);
  assert.equal((await observation.read()).status, 'fresh');
  await observation.stop();
});

test('uses subject.machine as the Hub envelope identity and keeps catalog node data in the body', async () => {
  const { client: streamClient, observation } = source();
  await observation.start();
  const hostPartial = {
    ...partial({ body: { nodes: [{ id: 'catalog-node-a', units: [{ kind: 'llm' }] }] } }),
    subject: { machine: 'realm-machine-a', node: 'catalog-node-a' },
  };
  publishComplete(streamClient, hostPartial);
  const sample = await observation.read();
  assert.equal(sample.status, 'fresh');
  assert.equal(sample.view?.hosts['host-a']?.stream_subject, 'realm-machine-a');
  assert.deepEqual(sample.view?.hosts['host-a']?.nodes, [{ id: 'catalog-node-a', units: [{ kind: 'llm' }] }]);
  await observation.stop();

  const { client: nodeOnlyClient, observation: nodeOnlyObservation } = source();
  await nodeOnlyObservation.start();
  nodeOnlyClient.emit({
    kind: 'stream.partial',
    sub: FLEET_STREAM_SUB,
    partial: { ...partial(), subject: { node: 'catalog-node-a' } },
    receipt: { age_ms: 0, warm: false },
  });
  nodeOnlyClient.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage() });
  assert.equal((await nodeOnlyObservation.read()).status, 'gap');
  const broker = new FakeBroker();
  await fleetTick({ client: broker, fleetSource: nodeOnlyObservation, jobs: [], now: () => NOW });
  assert.equal(broker.count('plan'), 0);
  await nodeOnlyObservation.stop();
});

test('a reset is a gap until a new complete cut arrives', async () => {
  const { client: streamClient, observation } = source();
  await observation.start();
  publishComplete(streamClient);
  assert.equal((await observation.read()).status, 'fresh');
  streamClient.emit({ kind: 'stream.reset', sub: FLEET_STREAM_SUB, reason: 'watermark_gap' });
  assert.equal((await observation.read()).status, 'gap');
  publishComplete(streamClient);
  assert.equal((await observation.read()).status, 'fresh');
  assert.ok(streamClient.renewCalls >= 1);
  await observation.stop();
});

test('a warm cached cut stays stale until a live sample replaces it', async () => {
  const { client: streamClient, observation } = source();
  await observation.start();
  publishComplete(streamClient, partial(), coverage(), 0, true);
  assert.equal((await observation.read()).status, 'stale');
  publishComplete(streamClient);
  assert.equal((await observation.read()).status, 'fresh');
  await observation.stop();
});

test('partial count and serialized-byte bounds fail closed as gap', async () => {
  const { client: overBytesClient, observation: overBytes } = source();
  await overBytes.start();
  publishComplete(overBytesClient, partial({ body: { pad: 'x'.repeat(MAX_FLEET_PARTIAL_BYTES) } }));
  assert.equal((await overBytes.read()).status, 'gap');
  await overBytes.stop();

  const { client: tooManyClient, observation: tooMany } = source();
  await tooMany.start();
  for (let index = 0; index <= 256; index += 1) {
    tooManyClient.emit({
      kind: 'stream.partial',
      sub: FLEET_STREAM_SUB,
      partial: partial({ source: `daemon:host-${index}`, host: `host-${index}` }),
      receipt: { age_ms: 0, warm: true },
    });
  }
  tooManyClient.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: coverage({
    expected: Array.from({ length: 257 }, (_, index) => `daemon:host-${index}`),
    reporting: Array.from({ length: 257 }, (_, index) => `daemon:host-${index}`),
  }) });
  assert.equal((await tooMany.read()).status, 'gap');
  await tooMany.stop();
});

test('replacement cuts account for both committed and staged generations within fixed bounds', async () => {
  assert.equal(MAX_FLEET_HELD_PARTIALS, MAX_FLEET_PARTIALS * 2);
  assert.equal(MAX_FLEET_HELD_BYTES, MAX_FLEET_PARTIALS * MAX_FLEET_PARTIAL_BYTES * 2);
  const { client: streamClient, observation } = source();
  await observation.start();
  const sources = Array.from({ length: 256 }, (_, index) => `daemon:host-${index}`);
  const completeCoverage = coverage({ expected: sources, reporting: sources });
  const publishCut = () => {
    for (let index = 0; index < sources.length; index += 1) {
      const source = sources[index]!;
      streamClient.emit({
        kind: 'stream.partial',
        sub: FLEET_STREAM_SUB,
        partial: partial({ source, host: source.slice('daemon:'.length), body: { pad: 'x'.repeat(3_500) } }),
        receipt: { age_ms: 0, warm: false },
      });
    }
    streamClient.emit({ kind: 'stream.coverage', sub: FLEET_STREAM_SUB, coverage: completeCoverage });
  };

  publishCut();
  assert.equal((await observation.read()).status, 'fresh');
  publishCut();
  assert.equal((await observation.read()).status, 'fresh', 'a complete replacement remains valid while both bounded generations are held');
  await observation.stop();
});

test('the CLI command is fixed to the authenticated Hub account contract and has no token argument', () => {
  const args = benchdayFleetStreamCommand('subscribe', 'livestack-fleetd');
  assert.deepEqual(args.slice(0, 4), ['--json', 'client', 'streams', 'subscribe']);
  assert.ok(args.includes('--route') && args[args.indexOf('--route') + 1] === 'hub');
  assert.ok(args.includes('--scope') && args[args.indexOf('--scope') + 1] === 'account');
  assert.ok(args.includes('--contract') && args[args.indexOf('--contract') + 1] === FLEET_STREAM_CONTRACT);
  assert.equal(args.includes('--token'), false);
});

test('the HTTP control client has no fleet-view method and keeps planning on its dedicated route', async () => {
  const urls: string[] = [];
  const client = httpFleetClient({
    baseUrl: 'http://broker.test',
    fetchImpl: async (input) => {
      urls.push(String(input));
      return new Response('{}', { status: 200 });
    },
  });
  assert.equal('view' in client, false);
  await client.plan({ jobs: [] });
  assert.deepEqual(urls, ['http://broker.test/fleet/plan']);
});
