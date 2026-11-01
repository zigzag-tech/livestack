// usage: node run_proof.mjs <handler.mjs>   prints {"ok":true,"emitted":N} or {"ok":false,"error":"..."}
import { prove } from '../../../livestack_node/workloads/supervised/simulate.mjs';
const module = await import(new URL(process.argv[2], 'file://' + process.cwd() + '/').href);
try {
  const { emitted } = await prove(module.handler, module.SCENARIO);
  console.log(JSON.stringify({ ok: true, emitted: emitted.length, last: emitted.at(-1) }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: error.constructor.name }));
}
