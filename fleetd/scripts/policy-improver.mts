/**
 * `npm run policy-improver -- --once`: one run of the scheduler policy's improver
 * (src/policy/improver.ts). The systemd timer (task 6.4) is what makes it daily.
 */
import { main } from '../src/policy/improver.js';

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    console.error(`policy-improver failed: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
    process.exit(1);
  },
);
