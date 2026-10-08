## Context

See `proposal.md` and the new `compilation-verifier-execution-context` spec. Linux `PrivateTmp=yes` under a systemd user manager creates a user namespace; in that namespace the root-owned registry appears to have UID 65534, so the host verifier correctly refuses it. A read-only probe on ZZ-Joe reproduced UID 65534 in the user manager and UID 0 in a system-manager unit running as the same worker UID/GID, both with `PrivateTmp=yes`.

The worker already receives an authority-authenticated `compilation` assignment before it starts the handler. The host verifier derives the exact attempt unit from the existing worker and attempt IDs and checks peer containment, the current grant, and resource limits. It already resolves the unit through either systemd manager.

## Goals / Non-Goals

**Goals:**

- Preserve root ownership identity for the verifier registry throughout admitted Linux compilation attempts.
- Keep the handler unprivileged and retain its private temporary files, `NoNewPrivileges`, and existing cgroup limits.
- Fail closed if system-manager execution is unavailable.
- Leave runtime-only attempts on the current user-manager path.

**Non-Goals:**

- Change compilation policy, worker enrollment, verifier protocol, authority credentials, or placement.
- Disable `PrivateTmp`, weaken verifier ownership checks, or pass verifier paths through caller-controlled input.
- Change macOS launchd or Windows Job Object execution.
- Add durable state, identifiers, or decision-ledger records.

## Decisions

1. **Select system-manager execution from the worker's accepted assignment.** When the Linux worker has validated `assignment.compilation`, it passes a dedicated internal flag to `SystemdExecutor.start`. The flag is not read from the job payload or environment. `SystemdExecutor` selects `systemd-run --system` for this attempt, sets `User` and `Group` to the current worker UID/GID, and retains `PrivateTmp=yes`, the backend's existing `NoNewPrivileges` setting, CPU/memory/task/wall limits, and the existing cgroup ownership. Native compilation retains `NoNewPrivileges=yes`.

   Alternatives: disabling `PrivateTmp` would restore host ownership but remove the isolation this change introduced; accepting UID 65534 or a caller-selected registry would weaken the verifier's trust boundary. Neither is acceptable.

2. **Use the existing attempt identity and verifier.** The systemd unit name remains derived from the existing worker and attempt IDs. The root verifier continues to resolve the unit in both system and user managers, authenticate the socket peer, and check the current authority receipt. No second authorization channel or new persistent registry is introduced.

3. **Do not fall back.** A missing sudo/system-manager capability is a named infrastructure refusal before handler start. Retrying under the user manager would reproduce the untrusted registry failure and silently change the security boundary.

4. **Keep platform-specific supervisors unchanged.** The worker passes this flag only to the Linux `SystemdExecutor`; launchd and Job Object executors receive no new argument.

## Risks / Trade-offs

- **[System manager or sudo policy is unavailable]** → Fail closed with a named refusal; verify the enrolled worker's existing system-manager sudo path before roll-out.
- **[System-manager unit is not visible to verifier containment checks]** → The verifier already searches both user and system managers; add a real admitted compilation positive control after the release roll.
- **[A different task is routed to an unrolled Linux worker]** → Roll the compatible worker release to every eligible Linux compilation worker in idle-safe order before declaring the class healthy.

## Migration Plan

1. Add unit and real-systemd integration coverage for compilation and runtime-only assignments.
2. Build and hash an immutable worker release from the landed commit.
3. Drain one eligible Linux worker at a time, let accepted attempts and cleanup finish, stage the new release, restart only that idle worker, and re-enable claims.
4. Submit a normal admitted Rust check without a host selector. Preserve the job and attempt IDs and verify a passing bounded receipt.
5. Keep each prior release available for rollback. On failure, drain only the affected idle worker, restore its previous `PYTHONPATH`, restart it, and re-enable claims.
