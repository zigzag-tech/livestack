# Design — learned-gpu-footprint

Read first: `node-py/livestack_node/measure.py` (`ActivationTracker`,
`ActivationObserver`), `manager.py` (`ModelManager._load`, `run_scope`), `facade.py`
(`/residence`), `planner.py` (`_World.reserve`, `_admission_need`).

## 1. Who owns what (config rule)

| durable state | owner | where | written by |
|---|---|---|---|
| learned resident bytes per unit | the node process | `~/.cache/livestack/activation-<host>-<kind>.json` (`LIVESTACK_ACT_STORE`), key `resident` | `ActivationObserver.measure_load` on each real load; only raised |
| learned activation per unit | same file, key `units` (unchanged) | `ActivationObserver.end` per op; only raised |
| declared prior | operator | unit config (`resident_bytes` for image workers) | hand |

Ids crossing: the unit name (`ManagedUnit.name`) is the key in the store, in
`/residence`, and in the broker's `Unit.kind`. The store is keyed by
`_footprint_signature` (unit names + declared footprints), unchanged: an operator who
edits the declared number is saying the model changed, and learning restarts.

## 2. Measurement

- **Resident:** `measure_load(unit, load)` reads `reserved` and `allocated` before and
  after `ManagedUnit.load`, called from `ModelManager._load` only when the unit is not
  already loaded. Resident = max(Δallocated, Δreserved). Co-resident units and the CUDA
  context are in the baseline, so they are not attributed; the context is held by the
  process whether or not the unit is resident, and the broker sees it in measured free.
- **Activation:** `begin` resets peaks and records allocated + reserved; `end` records
  max(peak_allocated − base_allocated, peak_reserved − base_reserved).
- **No evidence:** Δ < 64 MiB on load is ignored (state stays `unmeasured`).

## 3. Rules (same as the host-memory ledger)

- **Prior:** declared footprint until the first resident measurement.
- **Raised only by evidence:** after the first, `max(prev, new)` for both values.
- **Bounded:** `known_units` = the process's unit table; other names are dropped on
  load and refused on record. One file, one `.corrupt` slot.
- **Failure ≠ absence:** missing file = absence (`unmeasured`). Unreadable/invalid file
  = failure: quarantined, `store_error` set, `/residence` `learned.state: "failed"`
  with `error`, declared prior in force. Save failure and meter failure are reported the
  same way (`learned.error`) and never fail the load.
- **Safe default:** declared prior; a unit whose engine reports its own cost
  (`measured_cost`, vLLM) is untouched.

## 4. Planner

`footprint` = learned resident; `activation_headroom` = learned activation. Admission
needs footprint + headroom (`_admission_need`), residence reserves headroom while
resident (`_resident_headroom`). With both measured (`"allocator"`) the device's static
`reserved` slack — which exists to cover unmodelled activation — is waived, like
`"vllm-startup"`. `"allocator-resident"` keeps the reserve.

## 5. Ledger obligation

None new. `HostBroker` decision text already carries
`footprint X GiB (<footprint_source>)`, so every placement row now says `allocator`
when it planned on a measurement.

## 6. Rejected

- **Device-wide free delta (NVML) around load.** Contaminated by any other process on
  the card; the allocator delta is this process's own.
- **Including the CUDA context in the unit.** It does not leave when the unit is
  evicted; charging it to the unit over-states what an eviction frees.
- **Dropping the declared footprint from the signature.** Some units have no
  attributes; a model swapped under the same name would keep a too-low value.

## 7. Rollout

New release dir on zz-joe; repoint `harmony-klein-{0,1}` with a drop-in to run
`python -m livestack_node.imagegen.klein` from it; restart only with
`/livestack/residence` `busy: false`; restore `resident_bytes` to 3e9 (the prior);
one real generation; verify `/livestack/residence` and the host broker's view say
`allocator`. Rollback: remove the drop-in.
