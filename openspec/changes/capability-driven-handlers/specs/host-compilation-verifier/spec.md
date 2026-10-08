## ADDED Requirements

### Requirement: One root verifier may serve every worker id enrolled to a host

A host MAY run one root-owned compilation launch verifier serving every worker id whose authority principal is enrolled to that physical host, configured by a root-owned host file naming the physical host, machine identity, authority endpoint, and the enrolled worker accounts and their state roots. Worker ids SHALL NOT be enumerated by root. For each request the verifier SHALL derive its per-worker data from the request's worker id only after confirming the peer's kernel uid is an enrolled account, SHALL require the peer's cgroup to lie inside the live attempt unit derived from that worker id and attempt id, SHALL obtain the authority's current-attempt receipt, SHALL verify the real resource limits against the reservation, and SHALL re-verify peer and unit after the authority exchange. The request SHALL NOT be trusted for identity: no field of it, signed or not, is evidence of the peer. An unenrolled account, worker id, or host SHALL be refused with the same named reasons as a missing per-slot verifier.

#### Scenario: A handler claims another worker's identity
- **WHEN** a process inside worker B's attempt unit sends a request naming worker A
- **THEN** the verifier looks up A's unit, finds the peer outside it, and refuses with `compilation_peer_outside_attempt`

#### Scenario: A non-enrolled local account tries to mint a receipt
- **WHEN** a process of an unenrolled uid names a live attempt id from its environment
- **THEN** the verifier refuses before any authority exchange

#### Scenario: A worker id is added on an enrolled host without root
- **WHEN** an operator adds a worker principal for the enrolled host and starts a worker under an enrolled account
- **THEN** its attempts can be verified with no change to any root-owned file

#### Scenario: Two requests arrive at once
- **WHEN** one request waits on the authority while another worker's request arrives
- **THEN** the second is answered within its own deadline and neither delays the other

### Requirement: The host verifier credential is bounded to its host

The authority SHALL accept an operator-configured verifier principal bound to one host enrolment, SHALL allow it to call only the launch-verification route, and SHALL honour a worker id in the request only if that worker's principal belongs to that host enrolment. It SHALL answer exactly as it does to the worker's own token and SHALL refuse every other route, including claim, report, renewal, completion, and upload. Per-worker tokens and per-slot verifiers SHALL remain valid.

#### Scenario: A verifier credential names a worker on another host
- **WHEN** the host credential asks about a worker enrolled to a different host
- **THEN** the authority refuses by name and the route reveals no attempt state

#### Scenario: A verifier credential calls a worker route
- **WHEN** it attempts claim, renewal, or completion
- **THEN** the authority refuses with a named authorization error

### Requirement: Enrolment is idempotent, checkable, and visible

An administrator-run `enroll-worker` tool SHALL generate each per-worker or host verifier artifact from the host registry, SHALL be safe to rerun without change when nothing drifted, SHALL refuse to overwrite a drifted artifact without an explicit flag, and SHALL provide a read-only `--check` that reports for every registered worker whether its unit is active, its socket answers, its registry entry matches, and its account matches. Workers SHALL report the verifier state as a fact so that a compilation-capable worker lacking a verifier appears in the roster by name.

#### Scenario: A slot is added to the host registry
- **WHEN** an administrator runs the tool after adding a worker to the registry
- **THEN** exactly that worker's artifacts are created, existing ones are untouched, and a second run changes nothing

#### Scenario: A forgotten slot
- **WHEN** a worker advertises a compilation handler but no verifier entry exists for it
- **THEN** its fact says `verifier: missing`, the handler is `withheld` with `verifier_missing`, and `--check` reports the same
