## Context
The declared source revision must match the installed tree. Hand editing the declaration can make that claim false and race active work.

## Decisions
The operator command owns a private bounded transaction journal and cooperating declaration lock. The host broker owns the exclusive lease; its lease ID crosses into the journal. The source commit and artifact manifest are immutable inputs. The command defaults to a read-only plan. Apply revalidates its inputs after admission, writes a private backup, atomically replaces the complete declaration file, restarts only the explicitly selected facade service, and records the outcome. A failed restart restores the exact original declaration only if the candidate digest still owns the file, then restarts the previous configuration. Unknown rollback stays named, never success.

Source/runtime preparation happens outside the live declaration: full git revision, clean tracked source, matching native artifact hashes, executable interpreter, dedicated run config and actual STRATA_VERSION. The command never fakes an old revision, changes other units, cancels reservations or starts an engine itself. The existing exclusive lease waits/refuses on active speech/training. Credentials are read from the existing private file only, never command arguments or output. All reads/counts, subprocesses, wait windows, journals and backups have explicit bounds. One private transaction directory per caller-selected operation is exclusive-created and contains a single backup and journal; the operator removes it after retention review.

## Risks
A whole-facade restart affects its declared device. The command refuses a service not matching the restricted harmony-llm service name, and requires a real whole-device lease before changes. Another writer invalidates the digest; no overwrite is attempted. Process/HTTP health alone does not prove GPU cessation or model quality.
