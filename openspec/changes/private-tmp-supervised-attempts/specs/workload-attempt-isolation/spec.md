# workload-attempt-isolation Specification

## ADDED Requirements

### Requirement: Linux attempts use private temporary filesystems

Every Linux workload attempt supervised by a systemd user unit SHALL receive private, writable `/tmp` and `/var/tmp` mounts for the lifetime of its process tree. The attempt SHALL retain its configured privilege boundary and resource limits, and files created in those mounts SHALL NOT appear in the host temporary-filesystem namespace.

#### Scenario: Restricted attempt creates temporary runtime paths

- **WHEN** a Linux attempt runs with `NoNewPrivileges=yes` and creates a marker under `/tmp/.X11-unix` and `/var/tmp`
- **THEN** the operation succeeds inside the attempt without privilege escalation
- **AND** both markers are absent from the host temporary filesystems
- **AND** the attempt retains `NoNewPrivs: 1`

#### Scenario: Attempt exits

- **WHEN** the supervised attempt and its process tree stop
- **THEN** the private temporary mounts are released with the systemd unit
- **AND** the existing attempt journal, cleanup, and result semantics remain unchanged
