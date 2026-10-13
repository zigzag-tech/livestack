import { supervised } from '../../../livestack_node/workloads/supervised/supervised.mjs';

export const CONTRACT = { max_silence_seconds: 60, min_interval_seconds: 1 };
export const SCENARIO = { exec: { duration_s: 600, chunk_gap_s: 20 }, wait: { duration_s: 40 } };

export const handler = supervised(CONTRACT)(async (ctx) => {
  await ctx.phase('prepare', () => ctx.wait(Promise.resolve('ready'), 'cache lookup'));
  await ctx.each(['a', 'b', 'c'], (item) => ctx.exec('build', [item]), 'targets');
});
