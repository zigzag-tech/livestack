## Purpose

Defines the isolated execution context required for Linux attempts that use a host-owned compilation verifier. The handler remains unprivileged while the verifier can authenticate host-owned metadata and the live attempt.

## ADDED Requirements

### Requirement: Compilation attempts preserve verifier host identity

A Linux attempt carrying an admitted compilation grant SHALL run with a filesystem and UID view in which the root-owned verifier registry remains root-owned, and SHALL remain inside the exact supervised attempt unit and cgroup. The handler SHALL run as the configured worker UID and GID with the backend's existing `NoNewPrivileges` policy and configured resource limits. Its `/tmp` and `/var/tmp` SHALL remain private to the attempt. If this context cannot be established, the worker SHALL fail the attempt by name and SHALL NOT retry it under a user manager or a weaker isolation mode.

#### Scenario: Authorized compilation uses the host verifier under private temporary storage
- **WHEN** an admitted Linux compilation attempt starts with a valid host verifier registry
- **THEN** the handler sees the registry as root-owned, remains in its attempt cgroup, runs as the worker UID/GID with its backend's configured `NoNewPrivileges` policy, retains its CPU, memory, task and wall limits, and has private `/tmp` and `/var/tmp`
- **AND** the root verifier authorizes only the live attempt and the reserved compilation classes

#### Scenario: Host verifier identity cannot be preserved
- **WHEN** the worker cannot start the compilation attempt in a host-identity-preserving context
- **THEN** the attempt fails with a named infrastructure reason before the handler launches
- **AND** the worker does not fall back to a user-manager attempt or accept a caller-supplied registry path

#### Scenario: Runtime-only attempt keeps the ordinary user-manager path
- **WHEN** a Linux assignment has no compilation grant
- **THEN** the worker uses its existing user-manager execution path with private `/tmp` and `/var/tmp`
