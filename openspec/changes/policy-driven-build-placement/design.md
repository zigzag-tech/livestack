# Design

Reuse the existing durable job, resource reservation and physical-host policy.
Resource profiles distinguish operation/build mode/target; delivery intent names
the consumer host and bounded expected output size. Only feasible workers are
ranked. Use measured profile/host execution and transfer evidence, expose unknown
costs conservatively and retain bounded decision explanations.

Same-host handoff uses a private bounded content store and an authority-confirmed
reference bound to owner, source, job, attempt/fence, host, digest, size and expiry.
It never accepts caller-supplied filesystem paths. Retention holds disk only;
execution reservations end after supervised cleanup. Remote jobs retain their
ordinary authenticated CAS delivery. Old clients retain their existing behavior;
new clients check capability support before requesting new semantics.

Validate with a real isolated authority, workers, SQLite and temporary artifact
stores. Cover local-fit, busy-local, oversized-local, unmeasured routes, expiry,
tampering, foreign owners, cancellation and a real permitted execution control.
No production test workloads or shared service restart is part of testing.
