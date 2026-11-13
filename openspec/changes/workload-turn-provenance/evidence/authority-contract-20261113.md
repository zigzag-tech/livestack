# Authority component evidence — 2026-11-13

Owner approved the contract on2026-11-13 at22:58Asia/Shanghai. This evidence is
component-level; it is not a deployment, a real Jingway producer journey or E9 completion.

## Checks

- `PYTHONPATH=node-py .../python -m pytest -q node-py/tests/test_workload_turn_provenance.py`:29 passed in13.52s before the final page-size case was added.
- Added page-size test executed separately with `-k source_metadata_cannot_expand`:1 passed,29 deselected in3.77s. Together all30 current provenance controls have passed.
- The combined earlier provenance/job-changes run had37 passes and one fixture failure: oversized stamped metadata correctly returned413 while the new test expected400. Corrected ONLY that expected status, then reran provenance controls. All11 existing job-changes regression cases passed in that combined run. The combined command is not claimed green.
- `git diff --check` passed.
- `openspec validate workload-turn-provenance --strict` passed.

## Real boundaries exercised

Authenticated HTTP over a real ephemeral server and SQLite, authority-stamped
producer/effective owner, ungranted and foreign-owner refusal, closed/oversized
source data refusal, exact-turn isolation and tied cursors, actual authenticated
worker claim and attempt projection, byte/page bound, indexed query plan,
idempotent conflict under concurrent HTTP submissions, additive legacy-database
migration/replay, and TTL clearing while preserving retained jobs.

Default24h/max7day provenance retention is enforced by indexed store expiry and
read-before-query expiry. No metadata is copied to persistent spec/payload/labels;
only the opaque idempotency hash persists. Operator grants remain default-deny.

## Pending

Full normal repository gates/CI, matching Jingway producer capture, consumer
pin integration and an actual isolated turn/job/worker/explanation journey.
No production credentials/config files changed, no authority restart/deploy,
no placement/model/worker choice and no local execution fallback added.
