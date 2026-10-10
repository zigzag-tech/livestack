# Source-bound provider owner entry

`python -m livestack_node.provider_installation --private-service-config SNAPSHOT --app polytts|polyasr` verifies configured adapter/source/environment bytes and interpreter identity without model loading. It does not qualify complete inference behavior.

After actual dependency, service-unit and owner authority gates pass, use `python -m livestack_node.provider_entry --private-service-config SNAPSHOT --app polytts|polyasr`. This service command can load models; do not use it as an offline check.

Snapshot settings and actual registered administrator policy are described in the companion ZZOPS `docs/provider-owner-activation.md`. The source manifest comes from Unchain's source-only preparer. The environment manifest contains absolute root/pythonExecutable, pythonSha256 and relative regular files {path,bytes,sha256}. It must match actual sys.prefix, interpreter bytes and the complete declared file inventory. Unsupported directory/external links, undeclared files, changed bytes or more than65536 files/32GiB refuse. Stage the environment correctly; do not weaken validation or modify the active shared venv. Base Python/OS/CUDA qualification remains separate.

The owned0700 parent/0600 Unix socket authenticates Linux peer UID against private configuration custody. Public HTTP/profile credentials cannot obtain opaque authority. Held startup and authenticated shutdown carry narrowly scoped admission through actual ASGI lifecycle and executor/barrier settlement. Copied grants expire. No finite model deadline is promised.

The registry remains process memory. Unknown lifecycle or lost control acknowledgement requires durable owner reconciliation and must not cause resubmission or an acknowledged successful stop. Automatic activation resume, full native server startup, physical GPU cancellation and second-caller behavior remain unqualified.

The observed target TTS environment records38672 files/9.74GB and ASR35095/9.06GB; the previous16384-file cap was therefore explicitly raised to65536. Environment manifests are bounded to32MiB. Declared internal directory aliases use links[{path,target}], must match exact symlink bytes and resolve inside the sealed environment; unrecorded/external aliases refuse. The standard lib64->lib alias is supported without copying a shared editable dependency.

The environment manifest also requires `runtimeTree: {root, files, links}` for
the independent Python base runtime. Its root must equal the running
interpreter's `sys.base_prefix`; every regular stdlib/shared-library file is
hashed, and every alias has its exact relative path and link target declared.
Aliases must resolve inside that runtime. The bound is 65,536 regular files
and 4,096 internal aliases (the observed standalone runtime includes about
1,049 terminfo aliases). Unrecorded files, changed bytes, and external aliases
refuse admission. This source gate alone does not qualify an installed service.
