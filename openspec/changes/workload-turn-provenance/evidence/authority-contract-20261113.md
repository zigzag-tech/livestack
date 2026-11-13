# Authority component evidence — 2026-11-13

Owner approved2026-11-13 22:58Asia/Shanghai. Component evidence only: no
production deploy, actual Jingway producer journey or E9 completion claimed.

## Current checks

- Combined `PYTHONPATH=node-py .../python -m pytest -q node-py/tests/test_workload_turn_provenance.py node-py/tests/test_workload_job_changes.py`:43 passed in16.78s (32 provenance plus11 existing regressions).
- Subsequently added independent idle-sweep control: `-k normal_sweep`:1 passed,32 deselected in0.70s. Runtime code did not change between these runs. All33 current provenance cases and11 existing regressions have passed across these focused runs.
- Strict OpenSpec validation and diff checks passed.
- Earlier byte-bound fixture expected400; the service correctly returned413. Corrected the expectation only, then all affected controls passed. No size guard weakened.

## Boundaries exercised

Real authenticated HTTP/SQLite: source-kind opt-in grants; principal/effective
owner stamps; malformed/forged/oversized source refusal; owner, producer and
turn isolation; exact-turn-only producer reads; tied cursors and indexed query
plan;30KiB wire cap; actual authenticated worker claim/attempt projection;
concurrent idempotency conflict; compatible migration/replay; read-time TTL;
and independent normal sweep with retained job preservation.

Metadata is at most512bytes/job, inherits1000active/10000terminal default job
caps, and expires after24h by default (max7days). No duplicate plaintext metadata
in spec/payload/labels. The opaque idempotency hash persists; job retention
exemptions are untouched. Source grants remain default-deny.

## Pending

Full normal repository gates/CI, operator rollout/grant, matching Jingway producer,
consumer pin/wiring and an actual isolated turn/job/worker/explanation proof.
No credentials/config files changed, no authority restart/deploy, no planner or
worker/model choice, and no local execution fallback added.
