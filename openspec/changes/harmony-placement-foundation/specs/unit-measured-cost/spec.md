## ADDED Requirements

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
