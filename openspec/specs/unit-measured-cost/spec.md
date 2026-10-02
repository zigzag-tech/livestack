# unit-measured-cost Specification

## Purpose
A unit's memory cost is what its engine reported at startup, and admission uses it without double-counting the device reserve.

## Requirements

### Requirement: A unit's memory cost is what its engine reported, reported beside the admission number

After a unit's engine reports ready, the node SHALL parse the engine's own startup memory
report (weights and non-torch memory, peak activation, CUDA-graph memory, KV-cache bytes,
KV-cache tokens, and maximum concurrency at the configured context length). The node
SHALL report the result on `/residence` as the unit's measured cost, stamped with the
unit's `composition_hash`, the engine version, the measurement time, and
`source: "vllm-startup"`. It SHALL include the minimum footprint (weights, activation,
CUDA graphs and KV for one request of the configured length), because the KV pool
beyond that is elastic. It SHALL NOT replace the admission `footprint` while the
broker's device reserve also covers activation.

#### Scenario: Measured cost is reported beside the declared figure
- **WHEN** `llm_general` starts with adapters `chips-settinghead-v1` and `jemm` and
  `--kv-cache-dtype fp8`, and vLLM logs 18.39 GiB consumed, 2.56 GiB peak activation,
  0.90 GiB CUDA graphs and 37,981 KV tokens
- **THEN** `/residence` reports those values under `measured`, with
  `source: "vllm-startup"`, the whole footprint (23.51 GiB) and the minimum footprint
  (KV for one 24,576-token request)
- **AND** `footprint` stays the declared prior, labelled `footprint_source: declared`,
  until the device reserve stops double-counting activation (design §8b)

### Requirement: A declared footprint is a prior, labelled as one

Wherever a declared footprint appears (in `/residence`, in snapshots, or in ledger rows)
it SHALL carry `source: "declared"`, including while a measurement is reported beside it.

#### Scenario: Composition changed, not yet measured
- **WHEN** a unit's adapters change, so its `composition_hash` differs from every stored
  measurement, and the engine is still loading
- **THEN** its reported footprint is the declared value with `source: "declared"`

### Requirement: An unparsed measurement is unknown, never zero

If the engine becomes ready but its memory report cannot be parsed, the node SHALL report
`measured: "unknown"` with the names of the lines that did not match, and log the
failure. Composition SHALL treat that unit's cost as unknown. It SHALL NOT use 0.

#### Scenario: Engine upgrade changes the log format
- **WHEN** a new vLLM version prints its KV-cache line in a new format
- **THEN** `/residence` shows `measured: "unknown"` naming `kv_cache_size`
- **AND** composition rates every candidate that needs that measurement `unknown`

### Requirement: A node learns a unit's GPU footprint from its own allocator

A node that serves units through `attach()` with a CUDA allocator SHALL measure, on
every real load, the growth in its allocator's reserved (else allocated) bytes, and on
every op, the peak reserved and allocated growth over the op's baseline. It SHALL
persist both per unit. The first resident measurement SHALL replace the declared
footprint; later measurements SHALL only raise either value. A load that moves the
allocator by less than 64 MiB SHALL NOT be recorded. `/residence` SHALL report the
learned resident bytes as `footprint`, the learned peak as `activation_headroom`, and
`footprint_source` `"allocator"` when both are measured or `"allocator-resident"` when
only the load is. A unit whose engine reports its own cost SHALL be unaffected.

#### Scenario: Declared until measured
- **WHEN** klein is declared at 3e9 and has never loaded on this node
- **THEN** `/residence` reports `footprint` 3e9, `footprint_source: "declared"`, and
  `learned.state: "unmeasured"`

#### Scenario: Measured after one load and one generation
- **WHEN** klein loads (reserved grows 4.5e9) and generates (reserved peaks 2.7e9 over
  its baseline, allocated 1.9e9)
- **THEN** `/residence` reports `footprint` 4.5e9, `activation_headroom` 2.7e9 and
  `footprint_source: "allocator"`, and the broker's planner charges those values with
  no device reserve on top

#### Scenario: A smaller later measurement does not lower it
- **WHEN** after a restart klein loads with 3.2e9 reserved growth and a smaller peak
- **THEN** `footprint` stays 4.5e9 and `activation_headroom` 2.7e9

### Requirement: A lost measurement is reported, not mistaken for none

If the node's measurement store exists but cannot be read, or holds a value that is
not a finite byte count in `[0, 1 TiB]`, the node SHALL move it to `<store>.corrupt`,
plan the unit on its declared footprint, and report `learned.state: "failed"` with the
reason on `/residence`. The store SHALL hold entries only for units the process
serves.

#### Scenario: Corrupt store
- **WHEN** the store file is not JSON
- **THEN** `/residence` reports the declared footprint with `learned.state: "failed"`
  and an `error` naming the file, and `<store>.corrupt` holds the original bytes
