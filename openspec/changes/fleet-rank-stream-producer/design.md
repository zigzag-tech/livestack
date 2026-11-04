## Context

See proposal.md for motivation. The source is started by `hostd_streams` only for the observe-mode fleet broker. It already uses `StreamsRuntime` and `ReconnectingProducer`; the current alignment changes only the ranking contract projection and stream behavior.

The fleet broker's in-memory roster remains the source of ranking inputs. It owns no durable ranking cache. The authenticated daemon ingress and Hub registry own accepted stream state and its TTL. Ranking state is realm-scoped routing input, not account data.

## Goals / Non-Goals

**Goals:**
- Match the current `harmony.fleet-rank/1` subject and body contract exactly.
- Keep each vantage independent and each publication a complete snapshot.
- Preserve named failure and authority-refusal behavior while enforcing contract bounds.
- Carry a stable decision id into the Hub manifest's existing route-ledger correlation.

**Non-Goals:**
- Change the ranker, region eligibility policy, or target order.
- Add a polling or HTTP fallback, a source-side durable cache, or a second authority.
- Introduce per-account ranking data or identity keys.

## Decisions

1. **Key snapshots by `(kind, region, vantage)`.** Parse and retain up to 32 unique triples in configuration order. This permits multiple vantage views for the same kind and region while retaining deterministic behavior when the bound is reached. Excess valid keys are named as `fleet_rank_key_limit`; they do not displace the first 32.

2. **Project the existing ranker result into the strict stream body.** A present body carries `state`, the three key fields, `generated_at`, the 60-second TTL, `decision_id`, bounded `reason`, ordered `targets`, and preferences. It does not expose the legacy `chosen` alias. The `decision_id` is `fr_` plus SHA-256 of canonical JSON containing kind, region, generated time, ordered targets, and preferences. This preserves replay identity while distinguishing content changes made in the same second.

3. **Represent successful empty rankings as keyed tombstones.** The producer publishes `status: ok` with `state: removed` and `removed_reason: no_targets`. Computation, schema, byte-limit, and ingress errors remain named failures and never become removals or empty successful rankings.

4. **Use the existing authenticated producer session.** `ReconnectingProducer` registers `harmony.fleet-rank/1` through daemon ingress. Hub admission supplies the realm authority decision; the Livestack producer adds no credential, account scope, or alternate HTTP path. A refusal is logged by the existing callback and leaves the contract unavailable.

5. **Keep the decision ledger at the request decision boundary.** The source's periodic ranking is a complete realm-level advisory snapshot, not a placement. Its `decision_id` crosses into the Hub manifest, whose route record identifies the snapshot it followed. The existing `/fleet/rank` request endpoint continues to write its full candidate-set rank record. No new durable source state or ledger table is added.

## Risks / Trade-offs

- **The summary may exceed the contract's 256-character limit** → keep the prefix and append `...[truncated]`; individual target reasons remain separately bounded and available.
- **A stream authority is unavailable or already held elsewhere** → rely on the existing named registration refusal and do not fall back to `/fleet/rank` HTTP polling.
- **An invalid snapshot could replace a good one** → validate keys, target rows, nested values, and encoded size before sending a successful body; publish a named failed partial on validation errors.

## Migration Plan

1. Ship the producer contract alignment behind the existing observe-mode and configured-key gates.
2. Keep the legacy `/fleet/rank` endpoint available for existing clients; the stream consumer accepts only the strict current contract.
3. If the producer must be rolled back, revert the producer change and let the Hub expire the last accepted stream rows at their TTL. Do not introduce HTTP polling as rollback behavior.

## Open Questions

None.
