## Why

The FLUX.2-klein image worker on zz-joe (`harmony-klein-0/1`, RTX 2070 8 GB) is
planned with a footprint an operator typed. On 2026-10-02 its runtime changed (the NF4
text encoder now stays on the GPU instead of moving through host RAM), so the typed
number was hand-raised from 3e9 to 5e9 in `worker-{0,1}.json`. Measured that night:
steady VRAM 4.97 GB, generation peak reserved 7.2 GB at 768 px. Two defects made the
hand edit necessary and made it lossy:

1. **An `attach()` node's footprint is always declared.** `measure.ActivationObserver`
   learns each op's activation peak and persists it, but the resident weights it is
   measured against are the operator's number. Nothing measures what a load leaves
   on the card, so a runtime change is invisible to the planner until someone edits a
   config. `/residence` says `footprint_source: "declared"` forever.
2. **Activation is measured on `allocated`, not `reserved`.** What an op takes from
   the card is the allocator's reserved growth. At klein's 21:44 OOM, 2.8 GiB was
   allocated and another 4.2 GiB reserved-but-unallocated; an allocated-based
   headroom under-reserves exactly the memory that ran out.

Also: the learned store treated an unreadable file as an empty one (silent loss), and
editing the declared number (the only lever) silently discarded the learned
activation, because the store's signature includes it.

The klein worker itself (`~/harmony-image/klein/klein_worker.py`) was in no
repository.

**Design records realised:** `_plans/resource-planner.md` §2 ("`Unit.footprint` MUST be
measured weights + peak activation ... measure on first real run and cache it").
**Stale in it:** its header says `measure.py` implements weights + activation
measurement; it measured activation only, against declared weights. Same rules as the
host-memory ledger (`host-memory-ledger`, livestack `224dce34`): learned, persisted,
bounded, raised only by evidence, failure distinct from absence.

## What Changes

- `ActivationObserver.measure_load` measures the reserved (else allocated) growth
  across each real load; `ActivationTracker` persists it per unit beside the activation
  high-water. The first measurement replaces the declared prior; later ones only raise
  it. A load that moves this process's allocator by < 64 MiB (weights in another
  process, e.g. a vLLM proxy) is no evidence and is ignored, never recorded as 0.
- Activation = max(allocated growth, reserved growth) over the op's baseline.
- `/residence` reports the learned footprint, `footprint_source: "allocator"` (resident
  and activation both measured) or `"allocator-resident"` (no op yet), and a `learned`
  block (`declared_bytes`, `resident_bytes`, `activation_bytes`, `state`, `error`).
- `planner.MEASURED_SOURCES` gains `"allocator"`: the device reserve stops
  double-covering activation that `activation_headroom` already reserves.
- Store: one entry per unit the process serves (bounded); values outside
  `[0, 1 TiB]` or unreadable files are quarantined to `<store>.corrupt` and reported as
  `state: "failed"` with the reason; the declared prior stays in force.
- `livestack_node.imagegen.klein`: the klein runtime, GPU-resident text encoder, run
  as `python -m livestack_node.imagegen.klein`.

## Impact

- Every `attach()` node with CUDA torch starts learning resident bytes on its next
  load (polyasr, polytts, image workers). Until then it reports the declared footprint
  as before, plus a `learned` block saying it is unmeasured.
- Activation headroom can only grow (reserved ≥ allocated). Safe direction.
- Broker: no code change. `RestPeer` already plans with the reported footprint and
  passes `footprint_source` through.
