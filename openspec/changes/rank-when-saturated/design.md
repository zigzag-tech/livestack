## Decisions

**Saturated means no free slot**, read from the node's own admission queue
(`Queues.has_capacity`: in flight below `max_concurrent` and nobody waiting). The fleet view
carries the same figures per unit (`inventory.units.<unit>.queue`), which is what the ranker
reads for other nodes; unknown is never full.

**The ranking decides, not the trigger.** A saturated entry node only asks; when the
ranking chooses it (nothing finishes sooner, or nothing else is priced) the request queues
here exactly as before. So a full unit with a short wait keeps its traffic, and a 4 s cold
load wins only against a longer wait.

**Price by finish time once any warm target is full.** This reuses `finish_estimate`
(wait + load + first token + tokens / decode). For an indifferent request the expected
tokens are the caller's `max_tokens`, else `DEFAULT_EXPECTED_TOKENS`. Once a second copy is
warm, two warm copies also order by finish time while either is full; otherwise the
distance-first order stands (owner rule, `asr-rank-distance-first`).

**State and authority.** No new durable state. Residency stays with each host broker: a
`cold`-routed request is admitted and loaded by the target's own broker, and its idle unload
(fleet policy: unpinned, 60 s) removes the copy when traffic stops.

**Ledger.** Unchanged shape: the routing record carries `trigger: saturated`, each target's
`finish_s`, and the cold winner's "its load pays for itself" reason.

## Risks

- Thrash between copies: bounded by the measured wait. A copy wins only while its finish
  time is lower, and routed requests are never re-routed (`x-harmony-routed`).
- A stale view (up to the existing staleness bound) can price a queue that has drained;
  the cost is one cold load that the idle unload reclaims.
