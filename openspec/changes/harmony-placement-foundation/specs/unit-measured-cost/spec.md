## ADDED Requirements

### Requirement: A unit's memory cost is what its engine reported

After a unit's engine reports ready, the node SHALL parse the engine's own startup memory
report (weights and non-torch memory, peak activation, CUDA-graph memory, KV-cache bytes,
KV-cache tokens, and maximum concurrency at the configured context length). The node
SHALL report the result on `/residence` as the unit's measured cost, stamped with the
unit's `composition_hash`, the engine version, the measurement time, and
`source: "vllm-startup"`.

#### Scenario: Measured cost replaces the declared figure
- **WHEN** `llm_general` starts with adapters `chips-settinghead-v1` and `jemm` and
  `--kv-cache-dtype fp8`, and vLLM logs 18.39 GiB consumed, 2.56 GiB peak activation,
  0.90 GiB CUDA graphs and 37,981 KV tokens
- **THEN** `/residence` reports those values with `source: "vllm-startup"`
- **AND** the planner's `Unit.footprint` for that unit is the measured sum, not the
  declared `footprint_gb: 21`

### Requirement: A declared footprint is a prior, labelled as one

A declared footprint SHALL be used only while no measurement exists for the unit's
current `composition_hash`. Wherever it appears (in `/residence`, in snapshots, or in
ledger rows) it SHALL carry `source: "declared"`.

#### Scenario: Composition changed, not yet measured
- **WHEN** a unit's adapters change, so its `composition_hash` differs from every stored
  measurement, and the engine is still loading
- **THEN** its reported footprint is the declared value with `source: "declared"`

### Requirement: An unparsed measurement is unknown, never zero

If the engine becomes ready but its memory report cannot be parsed, the node SHALL report
`measured: "unknown"` with the names of the lines that did not match, increment a
visible counter, and log the failure. The planner SHALL treat that unit's footprint as
the device's whole budget until it is re-measured. It SHALL NOT use 0 or the declared
prior.

#### Scenario: Engine upgrade changes the log format
- **WHEN** a new vLLM version prints its KV-cache line in a new format
- **THEN** `/residence` shows `measured: "unknown"` naming `kv_cache_size`
- **AND** the planner does not place any other unit on that device on the strength of
  the missing number
