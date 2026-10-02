# Windows workload worker

Status: operational runbook. First enrolled 2026-10-02: `xc-win-1-native` on
`xc-win-1` (Windows 11 Pro 26200, 16 logical CPUs, 32 GB), beside that host's
WSL worker `xc-win-1-wsl`. Design and requirements:
`openspec/changes/windows-host-worker`.

## What runs where

```text
Service Control Manager
  -> LivestackWorkloadWorker (service, account .\harmony-worker, no admin rights)
     "C:\Program Files\Python312\python.exe" -m livestack_node.workloads.windows_service --config ...
     PYTHONPATH = immutable release (service registry value `Environment`)
     -> per attempt: Job Object  Global\livestack-harmony-work-<sha256(worker)[:16]>-<attempt>
        -> bounded_exec.py (created suspended, assigned, resumed) -> handler -> every descendant
```

- The worker imports only the standard library: it runs on the machine-wide
  CPython (`winget install Python.Python.3.12 --scope machine`), not a venv. A
  venv's `python.exe` is a launcher that runs the real interpreter as a child in
  its own nested job; the worker copes with it, but installed handlers and the
  service should name the base interpreter.
- Attempt limits are the job's: commit limit = `need.memory_bytes`, active
  processes = `max_tasks`, hard CPU rate = `need.cpu`. `harmony.probe.v1`
  reports them from the kernel (`isolation: windows-job-object`).
- Inspect a live attempt: the job name is in the handler's `HARMONY_JOB_OBJECT`;
  its run log is `<workspace>\<attempt>\output\command.log`.

## Install (idempotent)

As Administrator on the host:

1. Prerequisites: CPython 3.12+ machine-wide; Git; whatever the handlers need
   (Node, Rust/MSVC). `LongPathsEnabled` is set by the installer.
2. Stage an immutable release: `git archive <sha> node-py/livestack_node` into
   `C:\harmony\releases\livestack-<sha8>\node-py`; ACL it Administrators/SYSTEM
   full, Users read. Never edit a release in place; stage a new one.
3. Write the worker config (below) somewhere only Administrators can read.
4. Mint the authority principal (`role: worker`, the worker id, `host` = the
   PHYSICAL host name — the same `host` as a WSL worker on that machine), reload
   the authority (Benchday `docs/harmony-worker-enrolment.md`).
5. `node-py\deploy\windows\install-worker.ps1 -WorkerId <id> -Release <...\node-py>
   -Config <config.json> -WorkspaceGB 100`. It creates the account (random
   password, rotated on every run, known only to the SCM), grants it "Log on as
   a service" and "Create symbolic links" (handlers restore a captured tree's
   links), creates and mounts the bounded workspace VHDX, a SYSTEM boot task
   that re-attaches it, the locked-down state/config directories, and the
   service with restart-on-failure actions. It does not start the service.
6. `sc.exe start LivestackWorkloadWorker`; read
   `C:\harmony\state\<id>\worker.log`; confirm the worker in the authority's
   roster (`workers` table: `ready`, `report.handlers`).

Config shape (Windows specifics only; the rest is as on Linux):

```json
{
  "environment": {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows",
                  "COMSPEC": "C:\\Windows\\system32\\cmd.exe", "PATHEXT": ".COM;.EXE;.BAT;.CMD;.PY",
                  "PATH": "C:\\Program Files\\Python312;C:\\Windows\\system32;C:\\Windows"},
  "handlers": {"harmony.probe.v1": {"argv": ["C:\\Program Files\\Python312\\python.exe",
               "C:\\harmony\\releases\\livestack-<sha8>\\node-py\\livestack_node\\workloads\\probe.py"],
               "max_seconds": 60, "outputs": ["probe.json"]}},
  "labels": {"os": "windows", "rust_target": "x86_64-pc-windows-msvc"},
  "state_dir": "C:\\harmony\\state\\<id>",
  "workspace": "C:\\harmony\\work\\<id>",
  "workspace_bytes": 108447924224,
  "backing_filesystems": ["C:\\"]
}
```

The handler environment is exactly `environment` plus the Harmony variables:
Windows programs need `SystemRoot` (sockets and crypto fail without it) and
`PATHEXT`. The worker sets `TEMP`/`TMP` to the attempt's tmp directory.

## Compilation launch verifier

Handlers classified for compilation (`compilation_handlers` at the authority)
must be admitted per launch by the slot's verifier: on Windows a LocalSystem
service (`LivestackCompilationVerifier`) on `\\.\pipe\livestack-compilation-<worker>`.

1. Stage the verifier input (version, worker, host = the PHYSICAL host, authority
   as a numeric URL, the worker's token, journal =
   `C:\harmony\state\<worker>\active.json`) somewhere only Administrators read.
2. `node-py\deploy\windows\install-verifier.ps1 -Release <...\node-py> -Config <staged.json>`.
   It writes `C:\ProgramData\livestack\<worker>-verifier.json` (SYSTEM and
   Administrators only), merges the slot into `compilation-launch.json`, and
   starts the service. Log: `C:\ProgramData\livestack\verifier.log`.
3. The worker config needs `"compilation_launch_contract": 1`, and the handler
   environment `ProgramFiles`, `ProgramFiles(x86)`, `ProgramData`,
   `SystemDrive`: MSVC discovery fails without them (`link.exe not found`).
4. The authority must run code that knows the `windows` class BEFORE the policy
   names it, and the policy grants it per physical host.

The client refuses a pipe served by anything but LocalSystem, and a registry
any non-administrator could replace (`compilation_registry_untrusted`).

## Roll a release

Stage the new release dir (additive), wait until `<state_dir>\active.json` is
absent (no attempt), `sc.exe stop`, point the service's registry `Environment`
`PYTHONPATH` and the handler argv at the new dir (or re-run the installer with
`-Release`), `sc.exe start`. Roll back the same way to the old dir.

## Memory on a host that also runs WSL

`vmmemWSL` is an ordinary consumer in Windows' available memory, and WSL keeps
its guest page cache there indefinitely unless memory reclaim is on. Without it
the native worker reports almost nothing available (2026-10-02 on xc-win-1:
vmmem held 15.3 GB, 11 GB of it guest page cache; Windows had 4 GB available, so
the native worker offered 0.9 GB). Set in `%USERPROFILE%\.wslconfig`:

```ini
[experimental]
autoMemoryReclaim=gradual
```

It applies only after `wsl --shutdown`, which kills every Linux process: drain
the WSL worker first (`claim_enabled: false` on its principal, reload, wait for
no attempt), then shut down, start WSL, confirm the WSL worker re-registers,
re-enable claims. The WSL guest does not turn fstab swap entries into systemd
`.swap` units: swap FILES in `/etc/fstab` stay off after a restart unless a
oneshot unit runs `swapon -a` (xc-win-1: `wsl-fstab-swap.service`). Check
`swapon --show` after every `wsl --shutdown`. WSL itself is kept running by the
`WSL-Ubuntu-Boot` scheduled task (`wsl.exe ... sleep infinity`); start it again
after a shutdown (`Start-ScheduledTask WSL-Ubuntu-Boot`).

Placement charges every admission on the physical host against both workers
(they share `host`), so a WSL attempt's admission also reduces what the native
worker may take, even while that memory is already inside `vmmem`: conservative,
never over-booking.

## Gotchas met on the first enrolment

- **The OpenSSH login shell prints a banner**, which breaks `scp`/`sftp` to the
  Windows side. Copy files through the WSL guest (`scp -P 2222 ...
  ubuntu@<host>:/mnt/c/...`) and run PowerShell as `powershell -NoProfile
  -EncodedCommand <base64 UTF-16LE>`; errors arrive as CLIXML on stderr, so wrap
  the script in `try { } catch { Write-Output $_ }` to see them.
- **Windows PowerShell 5.1 does not escape quotes inside native arguments**:
  `sc.exe create ... binPath= "\"C:\Program Files\...\" ..."` reaches sc.exe
  broken and prints its usage text. The installer uses `New-Service`.
- **`icacls` with `(OI)(CI)` on a FILE** combined with `/inheritance:r` leaves
  the file with no ACE at all; inheritance flags are for directories only.
- **`secedit /export` names a resolvable account by name**, not `*SID`; check
  both before adding a right.
- **Windows `select()` takes sockets only**: the wrapper reads the handler's
  pipe on a thread.
- **Read-only FILES block deletion** on Windows whatever the directory allows;
  workspace removal clears the attribute.
- **The job memory limit is commit, enforced in granules**: a breach peak may
  pass the cap by under a MiB.
- **The authority sees a probe as "insufficient shared host resources"** on the
  native worker while a WSL attempt runs and WSL holds its cache: that is the
  shared-host arithmetic above, not a broken worker.
