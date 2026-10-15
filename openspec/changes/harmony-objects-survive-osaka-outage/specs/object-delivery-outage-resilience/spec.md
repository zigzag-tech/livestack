## ADDED Requirements

### Requirement: No single host is on the object-delivery critical path

Large-object delivery between a worker and the authority SHALL NOT depend on any one relay host. A worker outside the authority's region SHALL be configured with at least two independently hosted routes (doors, or a door plus another route kind). The loss of any one host SHALL reduce throughput or increase latency of object delivery and SHALL NOT make it impossible while another route is reachable.

#### Scenario: One door is down
- **WHEN** a worker has two doors and one host is unreachable
- **THEN** the transfer completes through the other door
- **AND** the trail names the unreachable door and its reason

#### Scenario: Only one route is configured
- **WHEN** a cross-region worker starts with fewer than two eligible routes
- **THEN** it reports `single_route` on its health surface and logs it at warning level

### Requirement: A door does not share fate with the control plane

A door that carries object bytes SHALL NOT run on the host that carries the mesh control plane (coordination server or DERP). Its byte budget SHALL be separate from the control-plane host's provider quota.

#### Scenario: The door host runs out of bandwidth
- **WHEN** a door host's provider suspends bandwidth
- **THEN** mesh coordination is unaffected by that event
- **AND** workers fall through to their other routes

### Requirement: Each door has its own tunnel and credentials

The authority SHALL hold one outbound tunnel per door, each failing independently, and each door SHALL have its own edge key. The tunnel MAY be a meshlink outbound attachment or an SSH reverse forward restricted to one listen. A door SHALL still forward only object routes to the single configured upstream and keep no object bytes.

#### Scenario: One tunnel drops
- **WHEN** the tunnel for door A drops
- **THEN** door B's tunnel and routes are unaffected
- **AND** the authority reports door A's tunnel down by name

### Requirement: Relay origins are configuration, not constants

No worker, client, publisher or workflow SHALL hard-code a relay hostname. Grant upload base URLs, publisher source upload origins and off-mesh workers' relay allowlists SHALL derive from the declared door list.

#### Scenario: A door is replaced
- **WHEN** an operator replaces a door in configuration
- **THEN** workers, grants and the iOS worker's allowlist use the new door without a code change

### Requirement: Fall-through to a throttled path is visible

A transfer that falls through to the mesh DERP path SHALL be marked `degraded_path: mesh_derp` in its trail.

#### Scenario: Both doors are down
- **WHEN** all doors fail and the authority is reached over the mesh
- **THEN** the trail carries `degraded_path: mesh_derp` and each door's failure reason
