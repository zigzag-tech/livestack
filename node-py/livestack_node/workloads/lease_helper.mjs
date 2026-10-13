// Node twin of lease_helper.report_progress (openspec attempt-progress-reporting, contract v1).
// A Node handler imports this by path from the Livestack checkout; it has no dependencies.
//
//   import { reportProgress } from '.../livestack_node/workloads/lease_helper.mjs';
//   reportProgress('run', { done: 37, total: 120, unit: 'assertions' });
//
// Writes $HARMONY_OUTPUT/progress.json atomically; the worker forwards it to the authority on its
// next heartbeat and the authority stamps all times. At most one write per second: faster calls keep
// only the latest, written when the second is up and again on process exit. `message` is for humans:
// never put credentials, tokens or URLs with secrets in it. done/total/unit/message need an authority
// that knows contract v1; phase, detail and fraction work on any.
import { renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export const PROGRESS_MIN_INTERVAL_MS = 1000;
const MESSAGE_BYTES = 200;
const TOTAL_MAX = 10 ** 7;
const NAME = /^[A-Za-z0-9_./:@-]+$/;

const state = { last: 0, pending: null, timer: null, exitHook: false };

// Same rules as model.progress() in Python; a bad value throws here instead of being silently dropped
// by the worker.
export function validateProgress(phase, fields = {}) {
  const unknown = Object.keys(fields).filter((k) => !['detail', 'fraction', 'done', 'total', 'unit', 'message', 'beat'].includes(k));
  if (unknown.length) throw new Error(`unknown progress field: ${unknown.join(', ')}`);
  if (typeof phase !== 'string' || phase.length < 1 || phase.length > 64 || !NAME.test(phase)) throw new Error('invalid progress phase');
  const value = { phase };
  const { detail, message, done, total, unit, fraction } = fields;
  if (detail != null) {
    if (typeof detail !== 'string' || detail.length > 1024) throw new Error('invalid progress detail');
    value.detail = Buffer.from(detail.replace(/[\x00-\x1f\x7f]/g, ' ')).subarray(0, MESSAGE_BYTES).toString('utf8').replace(/�+$/, '');
  }
  if (message != null) {
    if (typeof message !== 'string' || Buffer.byteLength(message) > MESSAGE_BYTES || /[\x00-\x1f\x7f]/.test(message)) {
      throw new Error('progress message must be one line of at most 200 bytes');
    }
    value.message = message;
  }
  for (const [key, count] of [['done', done], ['total', total]]) {
    if (count == null) continue;
    if (!Number.isInteger(count) || count < 0 || count > TOTAL_MAX) throw new Error(`progress ${key} must be an integer in [0, ${TOTAL_MAX}]`);
    value[key] = count;
  }
  if (fields.beat != null) {
    if (!Number.isInteger(fields.beat) || fields.beat < 0 || fields.beat > 2 ** 31) throw new Error('progress beat must be a non-negative integer');
    value.beat = fields.beat;
  }
  if (value.done != null && value.total != null && value.done > value.total) throw new Error('progress done must not exceed total');
  if (unit != null) {
    if (typeof unit !== 'string' || unit.length < 1 || unit.length > 24 || !NAME.test(unit)) throw new Error('invalid progress unit');
    value.unit = unit;
  }
  if (fraction != null) {
    if (typeof fraction !== 'number' || !Number.isFinite(fraction) || fraction < 0 || fraction > 1) throw new Error('progress fraction must be in [0, 1]');
    value.fraction = fraction;
  }
  return value;
}

export function writeProgress(value) {
  write(value);
}

function write(value) {
  const target = path.join(process.env.HARMONY_OUTPUT, 'progress.json');
  const temporary = `${target}.tmp`;
  writeFileSync(temporary, JSON.stringify(value));
  renameSync(temporary, target);
  state.last = Date.now();
}

export function flushProgress() {
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
  const value = state.pending;
  state.pending = null;
  if (value) write(value);
}

export function reportProgress(phase, fields = {}) {
  const value = validateProgress(phase, fields);
  if (!process.env.HARMONY_OUTPUT) throw new Error('HARMONY_OUTPUT is not set');
  const wait = PROGRESS_MIN_INTERVAL_MS - (Date.now() - state.last);
  if (wait <= 0 && !state.timer) {
    write(value);
    return;
  }
  state.pending = value;
  if (!state.timer) {
    state.timer = setTimeout(flushProgress, Math.max(wait, 0));
    state.timer.unref();
  }
  if (!state.exitHook) {
    state.exitHook = true;
    process.on('exit', flushProgress);
  }
}
