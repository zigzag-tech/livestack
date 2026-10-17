## Purpose

Defines how the one-per-realm Livestack workload authority accepts authenticated stream intents, resolves their bounded request objects, and publishes durable job outcomes with strict owner and admission checks.

## ADDED Requirements

### Requirement: Workload intents are bound to the authenticated owner and durable identity

The current one-per-realm workload authority SHALL derive the workload owner from the authenticated stream principal. A submit SHALL identify its workload with `intent_id`; the authority SHALL persist that identity before dispatch and use it as the WorkloadStore request key. A body-supplied owner SHALL NOT select another namespace.

#### Scenario: Valid referenced request is resolved before strict admission
- **WHEN** an authenticated principal submits an intent with a bounded `{ref, digest, bytes, media}` request reference
- **THEN** the authority reads the referenced object from that principal's CAS namespace and verifies `application/json`, the one-MiB size ceiling, exact byte count, SHA-256 digest, UTF-8 encoding and JSON object shape
- **AND** it passes only the verified object to normal WorkloadStore handler, selector, resource, principal and input-object admission
- **AND** the admitted WorkloadStore request key equals the stream `intent_id`

#### Scenario: Reference resolution refuses malformed or foreign data
- **WHEN** the reference is missing from the authenticated owner's CAS, names another owner's object, declares the wrong media, size or digest, exceeds the byte ceiling, or contains invalid UTF-8 or non-object JSON
- **THEN** the authority refuses the intent with a bounded reason
- **AND** no WorkloadStore job or attempt is admitted
- **AND** the terminal refusal records attribution `workload` for invalid or unauthorized input, or `infra` when the authority's required CAS service is unavailable

#### Scenario: Strict WorkloadStore policy refuses an intent
- **WHEN** the verified request names an uninstalled or unauthorized handler, invalid input object, or inadmissible selector/resource request
- **THEN** the authority refuses it through the stream intent result
- **AND** no execution attempt is admitted
- **AND** the terminal partial carries the refusal attribution and a bounded reason from the authority

#### Scenario: Same owner and intent replay is idempotent
- **WHEN** the same authenticated owner resubmits the same intent identity and body
- **THEN** the authority returns the current durable partial without creating or dispatching a second WorkloadStore job
- **AND** an owner mismatch or conflicting body under that identity is refused without changing the recorded workload

### Requirement: Terminal workload outcomes remain authority-published and attributed

Every accepted workload intent SHALL advance through the authority's durable progress ledger. A terminal refusal, failure, cancellation or expiry SHALL be published by the authority with attribution `workload`, `infra` or `unknown` and a bounded reason. A caller SHALL observe the authoritative terminal partial and SHALL NOT replace it with a locally inferred outcome.

#### Scenario: Backend outage is an infrastructure refusal
- **WHEN** WorkloadStore submission is unavailable or its service call fails
- **THEN** the authority records a terminal refusal with attribution `infra` and a bounded reason
- **AND** the refusal does not appear as a workload-policy rejection

#### Scenario: Caller cancellation is durable and idempotent
- **WHEN** the authenticated owner cancels its accepted intent before placement
- **THEN** the authority requests cancellation from WorkloadStore, records the resulting terminal partial and publishes its attribution and reason
- **AND** a repeated cancellation returns that recorded terminal outcome without changing it
- **AND** another owner cannot cancel the intent
