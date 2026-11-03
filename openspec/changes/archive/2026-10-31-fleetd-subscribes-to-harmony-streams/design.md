## Context

See proposal.md for motivation and specs/fleet-supervision-loop/spec.md for the
behavior contract. `fleetd/src/client.ts` currently owns both broker control
calls and `view()`, which performs `GET /fleet`. The Harmony stream is already
available through the authenticated Benchday resident CLI: account scope is
bound by the resident's device credentials, and `client events --follow` emits
ordered `stream.partial` records followed by `stream.coverage` records.

The Livestack `harmony.fleet/1` producer publishes host-level facts. Those facts
do not always include worker rows, so FleetD must preserve the raw host body and
leave placement to the existing `/fleet/plan` API.

## Goals / Non-Goals

**Goals:**

- Give the FleetD read path an authenticated account identity and a bounded,
  volatile stream observation.
- Stop planning when coverage is unknown, stale, degraded or incomplete.
- Keep broker HTTP calls for planning, operations and the ledger unchanged.

**Non-Goals:**

- Change Harmony's fleet producer, stream contract or account authorization.
- Recreate the worker roster from summaries that do not contain worker rows.
- Change `fleet-rank`, provisioning policy, or decision-ledger ownership.

## Decisions

1. **Use the Benchday headless resident as the Hub transport.** FleetD starts a
   dedicated foreground `benchday client` instance and uses its stored device
   credentials. It checks `client whoami` against a required expected account
   ID before subscribing with `--route hub --scope account --contract
   harmony.fleet/1`. This keeps secrets in Benchday's credential store. A direct
   unauthenticated WebSocket or HTTP read is rejected because neither proves
   account ownership.

2. **Separate stream observation from broker control.** `FleetClient` retains
   `/fleet/plan`, operation, and ledger methods but loses `view()`. A
   `FleetObservationSource` owns stream lifecycle and returns a discriminated
   observation. `fleetTick` gates planning on `fresh`; `read_fleet_view` reads
   this same source and refuses non-fresh results.

3. **Commit each event cut only at coverage.** The resident emits each partial
   before its coverage event. FleetD stages a cut, validates the contract,
   account, source set, partial size and status, then updates its bounded map
   only after complete coverage. Hub binds the host adapter's envelope to
   `subject.machine`; the catalog key `node` is a body-level fleet node field.
   Warm receipts, including missing receipt provenance, remain stale until a
   live sample replaces them. `stream.reset`, malformed framing, incomplete
   coverage and bound violations set `gap`; producer or lease failures set
   `degraded`. No partial cut is used for planning.

4. **Keep explicit memory and time bounds.** FleetD caps the event line at
   34,000 bytes, each committed and staged generation at 256 partials and 1 MiB,
   each serialized partial at 4,096 bytes, and both held generations together at
   512 partials and 2 MiB. It uses the stream receipt age, capped by the
   contract's 20-second TTL, rather than comparing producer wall clocks. A
   resident restart starts at `unknown` until a new complete sample arrives;
   stream state is intentionally not durable.

5. **Preserve raw host facts.** The derived view stores each source's body under
   its host identity, with any worker rows included by the body. If no `nodes`
   array exists, the consumer leaves it absent or empty; it does not synthesize
   `FleetNode` records. This is enough for FleetD's freshness gate and keeps the
   stream facts available to a read-only handback without making the stream a
   placement authority.

## Risks / Trade-offs

- [FleetD needs a Benchday CLI installation and enrolled account credentials] →
  make the expected account explicit, fail closed on missing identity, and
  document the required CLI instance configuration.
- [The stream may be temporarily silent during Hub reconnects] → stop planning
  after the 20-second receipt bound and recover only from a later complete cut.
- [Host-level facts may not contain the worker rows expected by older
  `read_fleet_view` consumers] → preserve the raw body and do not fabricate rows;
  `/fleet/plan` remains authoritative.

## Migration Plan

Deploy with a Benchday resident credential for the broker's account and set the
expected account ID. Start the stream source before the supervision loop. A
missing CLI, identity mismatch, refused lease or expired observation blocks
planning and is reported with its named state. Rollback restores the prior
FleetD version; the read path will resume the old `/fleet` polling behavior.
This change does not publish or deploy anything.
