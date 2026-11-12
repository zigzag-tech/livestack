## Context

Fresh source baseline is origin/main 1272b7e6. Older candidates disagree on
contract spelling, pair/triple keys, state tombstones and reason bounds. They
are reference material, not current truth. This proposal aligns the producer
with the exact bounded catalog proposed in Benchday; it must not be activated
against an older catalog/consumer.

## Decisions

The canonical contract is harmony.fleet.rank/1. It carries realm infrastructure
state, not user account data. Authenticated daemon ingress supplies the realm
registration fence; host-account aggregation is transport scope only. A second
authority is refused, and a disconnected/refused producer cannot fall back to
HTTP polling. The existing ReconnectingProducer owns reconnect and registration.

One configured vantage supplies each (kind, region). The subject contains only
those two fields; the body repeats them and includes vantage, generated_at,
ttl_s, decision_id, chosen, reason, targets and prefer. Pair strings are at most
32 characters, vantage 128; the producer may impose conservative byte bounds.
At most 32 configured pairs are active. Malformed and duplicate configuration
are logged with stable refusal codes; duplicate pair configuration never picks
a different vantage by publication arrival order. The 33rd pair is refused
without disabling the first 32. Input configuration itself is byte bounded.

Decisions are identified by SHA-256 of canonical JSON over kind, region,
vantage, generated_at, ordered targets, preferences and reason. Identical
content is a replay of the same decision; different content within the same
second has a different ID. The periodic snapshot is advisory measurement,
not an execution placement. The request route's ledger joins this identity.

The producer uses the existing ranker. ASR preference clauses remain
attribute/value for asr.streaming or asr.languages, and typed metric/direction
clauses with minimum sample and comparison identity. Eight hard eligibility
keys are not substituted for this grammar. Model/unit selector string bounds
belong to the eligibility boundary and must yield a named refusal, not clipping.
The source validates every returned preference and receipt before publication.

Bodies have at most 64 targets and 8 preferences, bounded nested inventory and
receipt values, and at most 28 KiB of encoded JSON, leaving envelope room under
the 32 KiB partial bound. No semantically relevant field is clipped. An
oversized or malformed result publishes a named failure. TTL is 60 s and never
exceeds the catalog's 600 s ceiling. Hub-clock expiry/future policy is enforced
by the Hub consumer, independently of the producer clock.

A successful ranker result with no eligible targets publishes a bodyless absent
partial, not a successful empty ranking and not a fabricated failure. Exceptions,
invalid data, reader failures and ingress refusals keep their named failure
values. The daemon/core absence support must be present in the exact-tree
fixture before this can qualify. No production activation is part of this change.

## Verification and rollout

Exercise the actual ranker and native producer over a private AF_UNIX ingress,
including reconnect, authority refusal, schema/byte bounds, empty vs failed
outcomes, pair overflow and same-second decision identity controls. Check valid
bodies against the declared catalog schema. Then hand the exact source commit
to the Benchday pinned fixture, remove its legacy poller/frame path, and require
exact-tree scoped Rust/Task-E2E receipts. Ship and archive only after the matching
source and consumer are integrated; status-only receipts do not establish this.
