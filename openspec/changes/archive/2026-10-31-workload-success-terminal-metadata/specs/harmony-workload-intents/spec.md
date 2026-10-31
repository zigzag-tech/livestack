## MODIFIED Requirements

### Requirement: Terminal workload outcomes remain authority-published and attributed

Every accepted workload intent SHALL advance through the authority's durable progress ledger. A terminal refusal, failure, cancellation, expiry or success SHALL be published by the authority with attribution `workload`, `infra` or `unknown` and a bounded, non-empty reason. A caller SHALL observe the authoritative terminal partial and SHALL NOT replace it with a locally inferred outcome. A successful completion SHALL be attributed to the workload only when its supervised exit receipt proves exit code zero and explicitly reports zero OOM-kill and process-limit events. The authority SHALL persist that evidence and the resulting attribution/reason with the job and SHALL expose the same terminal metadata through the stream and the hash-bound result manifest.

#### Scenario: Backend outage is an infrastructure refusal
- **WHEN** WorkloadStore submission is unavailable or its service call fails
- **THEN** the authority records a terminal refusal with attribution `infra` and a bounded reason
- **AND** the refusal does not appear as a workload-policy rejection

#### Scenario: Caller cancellation is durable and idempotent
- **WHEN** the authenticated owner cancels its accepted intent before placement
- **THEN** the authority requests cancellation from WorkloadStore, records the resulting terminal partial and publishes its attribution and reason
- **AND** a repeated cancellation returns that recorded terminal outcome without changing it
- **AND** another owner cannot cancel the intent

#### Scenario: Successful completion carries its supervised evidence
- **WHEN** the worker submits a successful completion with integer exit code `0` and explicit integer zero values for `resources.oom_kill` and `resources.pids_max_events`
- **THEN** the authority persists cause evidence containing those observed values, attribution `workload`, and reason `handler_exit_code_zero`
- **AND** the `done` partial publishes that attribution and reason
- **AND** the fetched result manifest includes the same attribution, reason and cause evidence under its output digest

#### Scenario: A success claim without a complete clean exit receipt is rejected
- **WHEN** the worker submits `outcome=succeeded` with a missing, malformed, non-zero or resource-limit exit receipt
- **THEN** the completion is rejected and the job does not transition to `succeeded`
- **AND** the authority publishes no `done` partial for that rejected completion

#### Scenario: A legacy success without usable receipt remains explicitly unknown
- **WHEN** the authority reads a previously stored successful job whose result lacks enough receipt data to establish clean completion
- **THEN** the terminal partial carries attribution `unknown` and reason `success_evidence_unavailable`
- **AND** the fetched result manifest carries the same explicit unknown classification
- **AND** the authority does not claim a clean workload success from the stored state alone
