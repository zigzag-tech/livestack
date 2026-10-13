// Node twin of simulate.py: the fake-clock proof that a supervised handler stays within max_silence.
export class FakeClock {
  constructor() { this.now = 0; }
  read() { return this.now; }
}

export class SimBackend {
  constructor(clock, scenario) { this.clock = clock; this.scenario = scenario || {}; this.calls = []; }
  advance(ctx, kind, chunked) {
    const spec = this.scenario[kind] || { duration_s: 1, chunk_gap_s: 1 };
    this.calls.push(kind);
    let remaining = spec.duration_s;
    const gap = spec.chunk_gap_s ?? 1;
    while (remaining > 0) {
      const step = chunked ? Math.min(gap, remaining) : Math.min(1, remaining);
      this.clock.now += step * 1000;
      remaining -= step;
      if (chunked) ctx.tick(); else ctx.flush();
      ctx.guard();
    }
  }
  async exec(ctx) { this.advance(ctx, 'exec', true); return { code: 0, tail: Buffer.alloc(0) }; }
  async wait(ctx, promise) { this.advance(ctx, 'wait', false); return promise; }
  async download(ctx) { this.advance(ctx, 'download', true); return 0; }
  async upload(ctx) { this.advance(ctx, 'upload', true); return 200; }
}

export async function prove(run, scenario, ...args) {
  const clock = new FakeClock();
  const emitted = [];
  const result = await run.rebuild({ mode: 'enforce', clock: () => clock.read(), backend: new SimBackend(clock, scenario),
                                     sink: (doc) => emitted.push(doc) })(...args);
  if (!emitted.length) throw new Error('a supervised handler must emit at least one document');
  return { emitted, result };
}
