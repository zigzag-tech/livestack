## Purpose

Defines how Livestack publishes current, source-owned observations that join compute resource identities to explicit Benchday host identities.

## ADDED Requirements

### Requirement: Harmony node identity reports require explicit configuration

A Harmony node SHALL report its stable node identity and Benchday host id only when both are explicitly configured and valid. It SHALL NOT derive either id from `host_id`, a hostname, peer URL, device id, or daemon id. Missing or invalid configuration SHALL omit the missing field and report a named absent status.

#### Scenario: Both identities are explicitly configured

- **WHEN** a Harmony node starts with a stable identity id and a Benchday host id
- **THEN** its capability report contains those explicit ids and a present identity status

#### Scenario: A node has only a logical host label

- **WHEN** a Harmony node reports `host_id` or `device_id` but no explicit Benchday host id
- **THEN** it publishes no `hosted_on` fact

#### Scenario: A node has no stable identity id

- **WHEN** a node has only a peer URL or host-and-port fallback identity
- **THEN** it publishes no `hosted_on` fact

### Requirement: The fleet broker owns Harmony node facts

The Livestack fleet broker SHALL publish a Fact v1 `hosted_on` edge only from a fresh capability observation containing an explicit stable node id and explicit Benchday host id. It SHALL identify the resource as `harmony:node:<id>`, the destination as `benchday:host:<id>`, and the authority as its configured Livestack fleet authority id. Each snapshot SHALL have a source generation and increasing sequence. Each Fact SHALL use the last successful capability observation time, a positive TTL no greater than 60 seconds, `attribute: present`, `value: true`, and `owner_account` scope. Conflicting host mappings for one stable node id SHALL be omitted and counted.

#### Scenario: A fresh, explicit node report is observed

- **WHEN** the fleet broker successfully reads a capability report containing both explicit identity ids
- **THEN** the next complete snapshot contains a Livestack fleet-authority Fact joining that node to the configured Benchday host

#### Scenario: A capability observation becomes stale

- **WHEN** the last successful capability observation is older than the Fact TTL
- **THEN** the next complete snapshot omits that node edge and reports it as stale

#### Scenario: Two nodes assert different hosts for one identity

- **WHEN** fresh capability reports use the same stable node id with different Benchday host ids
- **THEN** the fleet broker omits the ambiguous edge and increments the conflict count

### Requirement: Workload worker host facts use authority-owned principal mappings

The workload authority SHALL publish a worker-to-Benchday-host Fact only when the worker is currently registered and fresh and its authenticated worker principal contains an explicit Benchday host id. The authority SHALL bind the fact subject to the worker id from that principal and SHALL NOT accept a Benchday host id from an unauthenticated report body. A principal's worker, physical host, and Benchday host bindings SHALL remain immutable during principal reload. The authority id SHALL be explicitly configured; when missing or invalid, the endpoint SHALL refuse the snapshot.

#### Scenario: A registered worker has an explicit host mapping

- **WHEN** an authenticated worker with a configured Benchday host id has a fresh registration
- **THEN** the workload identity snapshot contains a `livestack:worker:<id> hosted_on benchday:host:<id>` fact

#### Scenario: An unregistered or unmapped worker is absent

- **WHEN** a worker is not registered, is stale, or its principal has no Benchday host id
- **THEN** no current host edge is published for that worker

#### Scenario: A worker report tries to change its mapping

- **WHEN** a worker report includes an unrecognized Benchday host field
- **THEN** registration rejects the report and preserves the principal-owned mapping

### Requirement: Identity snapshots are authenticated complete cuts

The fleet and workload identity endpoints SHALL require a configured authorized principal and SHALL return a complete bounded snapshot with a source generation and increasing sequence. Each Fact SHALL retain its source authority, scope, source observation time, fence, and TTL. A receiver SHALL be able to replace the previous snapshot from the same authority atomically; it SHALL NOT combine edges from different source sequences as if they were one current cut. An empty fact list SHALL represent a complete cut with no current mappings.

#### Scenario: A configured caller requests a snapshot

- **WHEN** an authorized caller requests the identity snapshot
- **THEN** it receives all current facts in one bounded response with source generation and sequence

#### Scenario: An unauthenticated caller requests a snapshot

- **WHEN** the endpoint is requested without a configured valid principal
- **THEN** it refuses the request with a named 401, 403, or 503 outcome

#### Scenario: The producer restarts

- **WHEN** the identity source restarts
- **THEN** it publishes a new generation and starts a fresh sequence, fencing snapshots from its previous process generation

### Requirement: Identity facts do not affect placement or admission

Identity facts SHALL be read-only metadata. The fleet and workload authorities SHALL NOT use them to choose a node, route, admission, or capability grant, and SHALL NOT write a placement decision-ledger record solely for publishing them.

#### Scenario: A host identity snapshot is read

- **WHEN** an identity endpoint returns a valid host edge
- **THEN** the existing placement and admission decisions remain unchanged and no decision-ledger record is added
