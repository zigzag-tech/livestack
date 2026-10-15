## ADDED Requirements

### Requirement: Abandonment is decided by stall, not by a fixed whole-response clock

A download or upload SHALL be abandoned on a route when no byte has moved for the route's stall window (default 20 seconds), or when a request exceeds a ceiling of `max(ceiling_minimum, nbytes / floor_rate)` with a default minimum of 60 seconds and a default floor rate of 64 KiB/s, where `nbytes` is the size of that request's range. The whole object has its own ceiling by the same formula over the object's size, with the operator's total-duration budget (default 3600 seconds) as its minimum. A fixed whole-response timer SHALL NOT bound a transfer that is making progress.

#### Scenario: A slow path keeps moving
- **WHEN** a 4 MiB block arrives at 30 KiB/s with no gap longer than the stall window
- **THEN** the transfer is not abandoned before the ceiling

#### Scenario: A path stalls
- **WHEN** no byte arrives for the stall window
- **THEN** the attempt is abandoned, the cause is named `stalled`, and the transfer resumes at the saved offset on this or the next route

#### Scenario: A trickle cannot hang forever
- **WHEN** throughput stays below the floor rate until the ceiling is reached
- **THEN** the transfer fails with a named duration cause, not a silent timeout

### Requirement: Block size follows measured throughput

The requested range size SHALL be at most 4 MiB and SHALL shrink toward 256 KiB while a route's recent throughput is below the slow rate (default 256 KiB/s), and grow back when it recovers. Every block is range-verified and the whole object digest-verified before it is visible.

#### Scenario: A slow route gets small blocks
- **WHEN** a route measures below the slow rate
- **THEN** subsequent ranges are smaller than 4 MiB and a connection reset costs at most one such block

### Requirement: Every client downloads resumably

Every client that downloads an object from the authority or a door, including the ZZOPS Node client, SHALL use ranged requests, resume at the verified offset after a failure and verify the whole-object sha256, rather than issue one unranged request.

#### Scenario: A reset mid-object
- **WHEN** the connection resets after part of an object is stored
- **THEN** the client continues from the stored offset and does not restart from byte 0

### Requirement: One rule, specified once and tested in every implementation

The stall window, ceiling, block-size rule and their constants SHALL be pinned by a single table-driven vector file. Each implementation (the livestack worker library in Python and the ZZOPS client in Node, which cannot import it) SHALL run a copy of that file in its own suite and SHALL NOT carry constants the file does not state. A change to the rule SHALL change the vectors first.

#### Scenario: The two implementations agree
- **WHEN** the shared vectors (ceiling, stalled/duration verdict, and a block-size sequence of successes and failures) are run in Python and in Node
- **THEN** both produce the vectors' expected values

### Requirement: Transfer evidence names what the rule did

The route trail of a transfer SHALL record the chosen block size, the smallest block used, and the number of `stalled` and `duration` abandonments.

#### Scenario: A stall is attributable
- **WHEN** a transfer abandons one attempt for `stalled` and then completes
- **THEN** its trail event reports one stall and the block size it ended on
