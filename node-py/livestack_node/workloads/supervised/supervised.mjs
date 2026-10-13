// Node twin of supervised.py; both implement SPEC.md and reproduce vectors.json.
// This module is the ONLY place handler-side process, network and sleep primitives live
// (tools/check-supervised-handlers.py forbids them in handler modules).
import { spawn } from 'node:child_process';
import { createReadStream, createWriteStream, statSync } from 'node:fs';
import { Readable } from 'node:stream';
import { writeProgress } from '../lease_helper.mjs';

const DEFAULT_PERIOD = { count: 6, seconds: 60 };

export class SilenceBreach extends Error {}

// ---- pure reducer (SPEC.md "Reducer"); time is an argument, in integer milliseconds ----------------
export function init(contract, t) {
  const period = contract.max_per_period || DEFAULT_PERIOD;
  return {
    contract: { ...contract }, count: period.count, seconds: period.seconds,
    min_interval_ms: Math.floor((contract.min_interval_seconds ?? 1) * 1000),
    doc: { phase: 'start' }, beat: 0, emitted: null, last_event: t, last_emit: null,
    milli_base: period.count * 1000, base_t: t, dropped: 0, pending: false,
  };
}

export function tokens(s, t) {
  return Math.min(s.count * 1000, s.milli_base + Math.floor(((t - s.base_t) * s.count) / s.seconds));
}

const wantedOf = (s) => (s.beat ? { ...s.doc, beat: s.beat } : { ...s.doc });
const same = (a, b) => a !== null && JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());

export function step(s, event) {
  const { t, type } = event;
  if (type === 'phase') {
    s.doc = { phase: event.name };
  } else if (type === 'done' || type === 'tick') {
    if (type === 'done') {
      for (const k of ['done', 'total', 'unit']) delete s.doc[k];
      for (const k of ['done', 'total', 'unit']) if (event[k] != null) s.doc[k] = event[k];
    } else {
      s.beat += 1;
    }
    if (event.message != null) s.doc.message = event.message;
  }
  if (type !== 'flush') s.last_event = t;
  const wanted = wantedOf(s);
  if (same(s.emitted, wanted)) { s.pending = false; return null; }
  if (tokens(s, t) >= 1000 && (s.last_emit === null || t - s.last_emit >= s.min_interval_ms)) {
    s.milli_base = tokens(s, t) - 1000; s.base_t = t;
    s.last_emit = t; s.emitted = wanted; s.pending = false;
    return wanted;
  }
  s.pending = true;
  if (type !== 'flush') s.dropped += 1;
  return null;
}

export const verdict = (s, t) => (t - s.last_event > s.contract.max_silence_seconds * 1000 ? 'overdue' : 'ok');

// ---- ctx -------------------------------------------------------------------------------------
export class Context {
  constructor(contract, { mode = 'report', clock = () => performance.now(), backend = null, sink = null } = {}) {
    this.mode = mode; this.clock = clock; this.t0 = clock();
    this.state = init(contract, 0);
    this.backend = backend || new ProcessBackend();
    this.sink = sink || writeProgress;
    this.breaches = 0; this.breached = false;
  }
  nowMs() { return Math.floor(this.clock() - this.t0); }
  event(type, fields = {}) {
    const doc = step(this.state, { ...fields, t: this.nowMs(), type });
    if (doc) this.sink(doc);
  }
  tick(message = null) { this.event('tick', { message }); }
  flush() { this.event('flush'); }
  guard() {
    if (verdict(this.state, this.nowMs()) !== 'overdue') { this.breached = false; return false; }
    if (!this.breached) {
      this.breached = true; this.breaches += 1;
      console.warn(`supervised handler silent for more than ${this.state.contract.max_silence_seconds}s (phase ${this.state.doc.phase})`);
    }
    if (this.mode === 'enforce') throw new SilenceBreach(`silent for more than ${this.state.contract.max_silence_seconds}s`);
    return true;
  }
  async phase(name, fn) { this.event('phase', { name }); return fn(); }
  async each(items, fn, unit = 'items') {
    const list = [...items]; const results = [];
    this.event('done', { done: 0, total: list.length, unit });
    for (const [index, item] of list.entries()) {
      results.push(await fn(item));
      this.event('done', { done: index + 1, total: list.length, unit });
    }
    return results;
  }
  exec(cmd, args = [], options = {}) { return this.backend.exec(this, cmd, [...args], options); }
  wait(promise, label) { this.event('tick', { message: String(label).slice(0, 200) }); return this.backend.wait(this, promise, label); }
  download(url, dest) { return this.backend.download(this, url, dest); }
  upload(url, path) { return this.backend.upload(this, url, path); }
}

export function supervised(contract, { mode = process.env.HARMONY_SUPERVISED_MODE || 'report', ...rest } = {}) {
  return (handler) => {
    const run = async (...args) => {
      const ctx = new Context(contract, { mode, ...rest });
      try {
        return await handler(ctx, ...args);
      } finally {
        if (ctx.state.pending) {  // the last held document is published regardless of the bucket
          const wanted = wantedOf(ctx.state);
          ctx.sink(wanted); ctx.state.emitted = wanted; ctx.state.pending = false;
        }
        if (ctx.state.dropped || ctx.breaches) console.error(`supervised: ${ctx.state.dropped} reports coalesced, ${ctx.breaches} silence breaches`);
      }
    };
    run.contract = contract;
    run.rebuild = (over) => supervised(contract, { mode, ...rest, ...over })(handler);
    return run;
  };
}

// ---- the real backend: the only raw primitives -----------------------------------------------
const POLL_MS = 250;
const TAIL = 64 * 1024;

export class ProcessBackend {
  exec(ctx, cmd, args, { env, cwd } = {}) {
    return new Promise((resolve, reject) => {
      const child = spawn(cmd, args, { env, cwd, stdio: ['ignore', 'pipe', 'pipe'] });
      let tail = Buffer.alloc(0);
      const pump = (chunk) => { tail = Buffer.concat([tail, chunk]).subarray(-TAIL); ctx.tick(); };
      child.stdout.on('data', pump); child.stderr.on('data', pump);
      const poll = setInterval(() => {
        try { ctx.flush(); ctx.guard(); } catch (error) { clearInterval(poll); child.kill('SIGKILL'); reject(error); }
      }, POLL_MS);
      child.on('error', (error) => { clearInterval(poll); reject(error); });
      child.on('close', (code) => { clearInterval(poll); ctx.tick(); resolve({ code, tail }); });
    });
  }
  wait(ctx, promise) {
    return new Promise((resolve, reject) => {
      const poll = setInterval(() => {
        try { ctx.flush(); ctx.guard(); } catch (error) { clearInterval(poll); reject(error); }
      }, POLL_MS);
      Promise.resolve(promise).then((v) => { clearInterval(poll); resolve(v); }, (e) => { clearInterval(poll); reject(e); });
    });
  }
  async download(ctx, url, dest) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`download failed: ${response.status}`);
    const total = Number(response.headers.get('content-length')) || null;
    const out = createWriteStream(dest);
    let done = 0;
    for await (const chunk of Readable.fromWeb(response.body)) {
      if (!out.write(chunk)) await new Promise((r) => out.once('drain', r));
      done += chunk.length;
      ctx.event('done', { done: total ? Math.min(done, total) : done, total, unit: 'bytes' });
      ctx.guard();
    }
    await new Promise((r) => out.end(r));
    return done;
  }
  async upload(ctx, url, path) {
    const total = statSync(path).size;
    let sent = 0;
    async function* body() {
      for await (const chunk of createReadStream(path)) {
        sent += chunk.length;
        ctx.event('done', { done: sent, total, unit: 'bytes' });
        ctx.guard();
        yield chunk;
      }
    }
    const response = await fetch(url, { method: 'PUT', body: Readable.from(body()), duplex: 'half', headers: { 'content-length': String(total) } });
    return response.status;
  }
}
