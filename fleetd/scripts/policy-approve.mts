/**
 * `npm run policy-approve -- <proposalId> --shadow|--activate` and
 * `npm run policy-revert -- <policyId>` (which passes `--revert`): a person's side of the
 * scheduler policy's improver (src/policy/approve.ts).
 */
import { main } from '../src/policy/approve.js';

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    console.error(`policy-approve failed: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
    process.exit(1);
  },
);
