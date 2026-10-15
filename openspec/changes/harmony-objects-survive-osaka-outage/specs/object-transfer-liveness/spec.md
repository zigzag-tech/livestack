## ADDED Requirements

### Requirement: Abandonment is decided by stall, not by a fixed whole-response clock

A download or upload SHALL be abandoned on a route when no byte has moved for the route's stall window (default 20 seconds), or when the transfer exceeds a ceiling of `max(ceiling_minimum, size / floor_rate)` with a default floor rate of 64 KiB/s. A fixed whole-response timer SHALL NOT bound a transfer that is making progress.

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
