# Proposal: Private temporary storage for supervised attempts

Linux worker attempts currently inherit the worker host's `/tmp` and `/var/tmp` mounts. A host-provided mount at `/tmp/.X11-unix` can therefore make display setup fail inside a restricted attempt: the child cannot replace the mount because `NoNewPrivileges` blocks `sudo`.

This change gives every Linux systemd-supervised attempt private `/tmp` and `/var/tmp` mounts. The task can create temporary files without modifying host temporary storage; its existing `NoNewPrivileges`, cgroup limits, process ownership, and explicit input/output paths remain in force.

Design record realised: `_plans/durable-workloads.md`, especially “Execution foundation evidence.” It currently describes cgroup and process supervision but omits the attempt's temporary-filesystem boundary. The record should state that systemd owns private temporary mounts for the lifetime of each Linux attempt, while durable inputs, execution configuration, logs, and outputs stay in their existing explicit paths.
