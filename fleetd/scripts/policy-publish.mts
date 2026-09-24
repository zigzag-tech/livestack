/**
 * `npm run policy-publish -- <artifact.json> [--confirm-objective]`: publish a hand-authored
 * artifact revision through the improver's activation ledger (src/policy/publish.ts).
 */
import { main } from '../src/policy/publish.js';

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    console.error(`policy-publish failed: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
    process.exit(1);
  },
);
