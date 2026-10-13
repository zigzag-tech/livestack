import { supervised } from '../../../livestack_node/workloads/supervised/supervised.mjs';
export const SCENARIO = { wait: { duration_s: 300 } };
export const handler = supervised({ max_silence_seconds: 60 })(async (ctx) => { await ctx.wait(Promise.resolve('x'), 'one very long step'); });
