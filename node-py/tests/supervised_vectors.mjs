// Runs vectors.json through the Node reducer and prints one JSON result per case (compared by pytest).
import { readFileSync } from 'node:fs';
import { init, step, verdict } from '../livestack_node/workloads/supervised/supervised.mjs';

const vectors = JSON.parse(readFileSync(new URL('../livestack_node/workloads/supervised/vectors.json', import.meta.url), 'utf8'));
const results = vectors.map((vector) => {
  const state = init(vector.contract, 0);
  const emits = [];
  for (const event of vector.events) {
    const doc = step(state, event);
    if (doc) emits.push({ t: event.t, doc });
  }
  return { name: vector.name, emits, dropped: state.dropped,
           verdicts: vector.expect.verdicts.map((v) => ({ t: v.t, state: verdict(state, v.t) })) };
});
console.log(JSON.stringify(results));
