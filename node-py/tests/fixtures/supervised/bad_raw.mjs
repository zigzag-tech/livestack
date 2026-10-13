import { spawn } from 'node:child_process';
import { supervised } from '../../../livestack_node/workloads/supervised/supervised.mjs';
export const handler = supervised({ max_silence_seconds: 60 })(async () => { await fetch('http://x'); setTimeout(() => {}, 1); });
