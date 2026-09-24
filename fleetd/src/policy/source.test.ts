/**
 * The policy record stream reader (task 5.1), over real files laid out the way the native
 * Recorder rotates them (`stem.jsonl`, `.1`, `.2`, … with the OLDEST highest).
 */
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import type { PolicyRecordLine } from 'jingway-framework/server/policy';

import { POLICY_ID, StreamPolicySource, type MissingRotationGap } from './source.js';

const decision = (id: string, ts: number) => ({
  record: 'policy_decision',
  decision_id: id,
  ts,
  policy_id: POLICY_ID,
  family: { id: POLICY_ID, version: 1 },
  artifact_version: 'b3:aa',
  context: {},
  candidates: [],
  rows: [],
  greedy: 'a',
  chosen: 'a',
  explored: false,
  explore_set: ['a'],
  propensities: { a: 1 },
  exploration: { enabled: false, epsilon: 0, margin: 0, draw: 0 },
  escalate: null,
  principal: 'acct',
  self_traffic: false,
});
const outcome = (id: string, ts: number) => ({
  record: 'policy_outcome', decision_id: id, outcome_id: 'job_wall_s', value: 1, ts, source: 'test',
});
const jsonl = (...lines: unknown[]) => lines.map((l) => (typeof l === 'string' ? l : JSON.stringify(l))).join('\n') + '\n';

async function streamDir(files: Record<string, string>): Promise<string> {
  const dir = path.join(await mkdtemp(path.join(tmpdir(), 'policy-source-')), 'records');
  await mkdir(dir, { recursive: true });
  for (const [name, body] of Object.entries(files)) await writeFile(path.join(dir, name), body);
  return dir;
}

async function readAll(source: StreamPolicySource, from: number, to: number): Promise<PolicyRecordLine[]> {
  const out: PolicyRecordLine[] = [];
  for await (const r of source.read({ from: new Date(from * 1000), to: new Date(to * 1000) })) out.push(r);
  return out;
}

const ids = (records: PolicyRecordLine[]) =>
  records.map((r) => (r.record === 'recorder_gap' ? `gap:${r.from_ts}-${r.to_ts}` : `${r.record === 'policy_decision' ? 'd' : 'o'}:${r.decision_id}`));

test('rotated files are read oldest first — numerically, not lexically', async () => {
  const stem = POLICY_ID;
  const files: Record<string, string> = { [`${stem}.jsonl`]: jsonl(decision('d0', 1000)) };
  // 11 rotations: a lexical sort would read .1, .10, .11, .2, …
  for (let i = 1; i <= 11; i++) files[`${stem}.jsonl.${i}`] = jsonl(decision(`d${i}`, 1000 - i));
  const source = new StreamPolicySource({ dir: await streamDir(files) });
  const got = await readAll(source, 0, 2000);
  assert.deepEqual(ids(got), ['d:d11', 'd:d10', 'd:d9', 'd:d8', 'd:d7', 'd:d6', 'd:d5', 'd:d4', 'd:d3', 'd:d2', 'd:d1', 'd:d0']);
  assert.deepEqual(source.lastRead.missing, []);
  assert.equal(source.pattern, path.join(source.dir, `${stem}.jsonl*`));
});

test('a missing rotated file inside the window is reported as a gap, never skipped', async () => {
  const stem = POLICY_ID;
  const source = new StreamPolicySource({
    dir: await streamDir({
      [`${stem}.jsonl.3`]: jsonl(decision('old1', 100), decision('old2', 110)),
      // .2 is gone: whatever it held between t=110 and t=300 was lost.
      [`${stem}.jsonl.1`]: jsonl(decision('mid', 300), outcome('mid', 305)),
      [`${stem}.jsonl`]: jsonl(decision('new', 400)),
    }),
  });
  const got = await readAll(source, 0, 1000);
  assert.deepEqual(ids(got), ['d:old1', 'd:old2', 'gap:110-300', 'd:mid', 'o:mid', 'd:new']);
  const gap = got[2] as MissingRotationGap;
  assert.equal(gap.reason, 'missing_rotation');
  assert.deepEqual(gap.missing, [`${stem}.jsonl.2`]);
  assert.equal(gap.dropped_unknown, true);
  assert.deepEqual(source.lastRead.missing, [`${stem}.jsonl.2`]);
  assert.equal(source.lastRead.gaps, 1);
});

test('a missing newest file is a gap up to the end of the window', async () => {
  const stem = POLICY_ID;
  const source = new StreamPolicySource({ dir: await streamDir({ [`${stem}.jsonl.1`]: jsonl(decision('d', 100)) }) });
  const got = await readAll(source, 0, 500);
  assert.deepEqual(ids(got), ['d:d', 'gap:100-500']);
  assert.deepEqual(source.lastRead.missing, [`${stem}.jsonl`]);
});

test('recorder_gap lines pass through; the window filters decisions but keeps later outcomes', async () => {
  const stem = POLICY_ID;
  const recorderGap = { record: 'recorder_gap', dropped: 12, from_ts: 150, to_ts: 160 };
  const outsideGap = { record: 'recorder_gap', dropped: 3, from_ts: 10, to_ts: 20 };
  const source = new StreamPolicySource({
    dir: await streamDir({
      [`${stem}.jsonl`]: jsonl(
        decision('before', 50),
        outsideGap,
        decision('in', 150),
        recorderGap,
        'not json at all',
        '{"record":"something_else"}',
        outcome('in', 900),
        decision('after', 950),
      ),
    }),
  });
  const got = await readAll(source, 100, 800);
  assert.deepEqual(ids(got), ['d:in', 'gap:150-160', 'o:in']);
  assert.equal((got[1] as { dropped: number }).dropped, 12);
  assert.equal(source.lastRead.unparseable, 2);
  assert.equal(source.lastRead.decisions, 1);
  assert.equal(source.lastRead.outcomes, 1);
});

test('no records directory is an error, not an empty window', async () => {
  const source = new StreamPolicySource({ dir: path.join(tmpdir(), 'definitely-not-a-policy-dir', 'records') });
  await assert.rejects(readAll(source, 0, 1), /is not readable/);
});
