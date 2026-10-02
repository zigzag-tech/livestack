## ADDED Requirements

### Requirement: Units are served by a declared engine, vLLM by default

A harmony-llm unit SHALL name its engine in `engine` (`"vllm"` when absent). The node
SHALL start, probe, measure and stop the unit through that engine's adapter, and SHALL
NOT expose `engine` as an attribute a request can require. A unit naming an engine the
node does not have SHALL be reported unavailable with that reason, and requests for it
SHALL be refused with that reason rather than routed elsewhere.

#### Scenario: Existing vLLM units are unchanged
- **WHEN** `llm_general` is declared without `engine`
- **THEN** it starts with the same `vllm serve` argv as before this change and reports the same measured cost

#### Scenario: Unknown engine is refused by name
- **WHEN** a unit declares `engine: "foo"`
- **THEN** `/residence` reports it unavailable with reason `unknown engine foo`, and a request for it answers 503 with that reason

### Requirement: An engine build is pinned and verified

A unit using a non-vLLM engine SHALL declare `engine_source` with a repository and a
full revision, plus the model artefact and its hash. At node start the node SHALL
verify the installed engine revision and model hash; on mismatch the unit SHALL be
unavailable with a reason naming both values.

#### Scenario: Wrong revision installed
- **WHEN** `flash_next` pins revision A and the installed Strata tree is at revision B
- **THEN** the unit is unavailable with reason naming A and B, and no request loads it

### Requirement: Residency is caused only by requests

A unit SHALL become resident only because the planner admitted it for a request (or
for its residency tier's restore rule). Operators and agents SHALL NOT force a load
through the engine's own load endpoint, a warm endpoint, or a manual process start.
An unpinned non-default unit SHALL NOT be loaded at node start.

#### Scenario: Node start leaves the unit cold
- **WHEN** harmony-llm starts with `flash_next` declared UNPINNED, not default, not warm_on_start
- **THEN** hostd `/status` lists `flash_next` with `resident: false`

### Requirement: Host RAM is a planned, host-scoped resource

A unit footprint MAY include `ram_bytes`. The planner SHALL admit a placement only if
it fits the device's free capacity and the free `ram_bytes` of the device's host,
shared by every device on that host. Evicting a unit SHALL return its `ram_bytes` to
the host pool. When the host's memory is unmeasured, a unit with `ram_bytes` SHALL be
refused with reason `host memory unmeasured`; units without `ram_bytes` SHALL place
as before. Every placement record for a unit with `ram_bytes` SHALL carry the need,
the host free figure and the reserve.

#### Scenario: Two RAM-heavy units on one host
- **WHEN** two units each needing 45 GB of `ram_bytes` are requested on two different cards of a host with 64 GB free
- **THEN** the second is not admitted while the first is resident, and the refusal names the host pool arithmetic

#### Scenario: Unmeasured host
- **WHEN** the broker has no host memory measurement for a host
- **THEN** a unit with `ram_bytes` is refused there with `host memory unmeasured`, and units without `ram_bytes` are placed as before

### Requirement: A unit may claim a whole device

A unit declaring `exclusive_device: true` SHALL be charged the device's entire
capacity. It SHALL be admitted only when every other tenant on the device is
evictable; an idle evictable tenant SHALL be evicted, a busy one SHALL defer the
admission, and a HARD_PIN tenant SHALL cause a refusal naming that tenant.

#### Scenario: Exclusive unit displaces an idle unpinned LLM
- **WHEN** `llm_general` (UNPINNED, idle past its min residency) is the only tenant of card 1 and a request needs `flash_next`
- **THEN** the planner evicts `llm_general` and admits `flash_next` on card 1

#### Scenario: Exclusive unit blocked by a hard pin
- **WHEN** an exclusive unit could only be placed on a device holding a HARD_PIN tenant
- **THEN** the request is refused with a reason naming the tenant and its residency

### Requirement: Units advertise derived concurrency and are queued at it

Every LLM unit SHALL advertise `max_concurrent`, derived from its engine's launch
line, never hand-declared. The node SHALL keep at most `max_concurrent` requests in
flight to a unit, hold the excess in a bounded queue, and refuse with 429 and a reason
beyond that bound. The queue depth SHALL be reported on `/residence`. The
reuse-a-resident-unit shortcut SHALL apply only while the resident unit is below its
`max_concurrent` with an empty queue.

#### Scenario: Single-stream unit does not absorb broad traffic
- **WHEN** `flash_next` (max_concurrent 1) is resident and busy, and a broad `require:class=llm` request arrives
- **THEN** the request is not queued behind it by the shortcut; it goes to the planner like any non-resident choice

#### Scenario: Concurrency requirement selects the concurrent unit
- **WHEN** a request requires `max_concurrent=[8,]`
- **THEN** only `llm_general` satisfies it, and `flash_next` is never selected for it

### Requirement: Broad requests choose between engines by characteristics

Requests SHALL be able to select between units of different engines using broad
characteristics (`context_len`, `params_b`, `active_params_b`, `arch`,
`max_concurrent`) in `require`, and the LLM preference vocabulary
(`llm.params_b`, `llm.context_len`, `llm.decode_tok_s`, `llm.first_token_ms`) in
`prefer`. A hard requirement the resident unit fails SHALL cause a swap if another
unit satisfies it. A preference alone SHALL NOT cause a swap.

#### Scenario: Long context loads Flash-Next without naming it
- **WHEN** `llm_general` (context 24,576) is resident and idle, and a request says `require:class=llm,context_len=[131072,]`
- **THEN** Harmony evicts `llm_general`, loads `flash_next` through Strata, and answers from it

#### Scenario: A request both satisfy does not swap
- **WHEN** `llm_general` is resident and a request says `require:class=llm` with `prefer` `llm.params_b: max` and no time budget
- **THEN** `llm_general` answers and nothing is evicted, and the selection record carries the preference receipt

#### Scenario: Named local still reaches the 27B
- **WHEN** `flash_next` is resident and a request says `model: "local"`
- **THEN** it is served by `llm_general` (after the planner evicts the idle `flash_next`), never by `flash_next`
