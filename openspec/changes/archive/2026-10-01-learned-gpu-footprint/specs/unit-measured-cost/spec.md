## ADDED Requirements

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
