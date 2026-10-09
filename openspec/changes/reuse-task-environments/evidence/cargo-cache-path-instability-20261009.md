# Cargo cache reuse and attempt path evidence — 2026-10-09

## Admitted runs

Two successful admitted Rust `check cli` jobs used handle
`4636512da5084b70bc2f0a1b0f045020` on `zz-joe-e2e-2`:

| Job | Source | Cargo cache receipts | Queue | Compile |
|---|---|---|---:|---:|
| `36de28bc5bc14f02a6cf9bcfc7deb716` | `2db5a26faec368cdb46b040b15d6b18f95a8ad2b` | `cargo-home`, `cargo-target`: reused | 1,130.046 s | 32.603 s |
| `bbbe018da9c2400c9a1774d39028feeb` | `fcba798add0ca8a979297306e9d00397c0fd4581` | `cargo-home`, `cargo-target`: reused | 625.394 s | 34.859 s |

The source diff contains 75 paths and no Rust source changes. The second run's
`command.log` contains Cargo `Compiling` lines for a broad dependency graph, so
the cache-component `reused` receipt does not prove that Cargo reused compiled
artifacts. The second log digest is
`c1d48f50f443c84248c0ddfc7d4541f0faa0798b73379a1e2aac34fb17c73235`; its
`rust-check.json` digest is
`e5ccbb17b9a54dc6dbdc8745b641242f6ea16506954479a079c653a8bcababd3`.

## Path finding and code change

Before this change, `worker.py` bound the retained source to
`<workspace>/<attempt-id>/environment-view` and passed that path as both the
handler working directory and `HARMONY_INPUT`. The attempt-specific path also
determined `CARGO_TARGET_DIR`. Cargo fingerprints therefore saw a different
absolute workspace/target path on the next request even though the underlying
cache bytes remained in the same logical environment. This is the leading
explanation for the rebuild; it has not yet been isolated by a controlled
compiler run.

The worker now binds source at `task-environment-view` beside the shared
physical-host environment mount. Provisioning creates and validates that
empty, worker-owned mode-`0700` directory. `TaskEnvironmentStore` refuses an
unsafe/missing production view; user-owned fixture roots can create the view
for local worker tests. Per-attempt systemd mount namespaces keep concurrent
jobs isolated while presenting the same absolute source and cache paths across
worker identities on one host.

## Remaining evidence

The focused Livestack checks passed 30/30 on `lappy-bellinzona` with Python
3.13.15 in 12.47 seconds: all of `test_task_environments.py` plus
`test_worker_reuses_task_environment_across_captured_source_edits`. Pytest's
temporary root was placed under `/home/ubuntu/.cache` because systemd
`PrivateTmp` hides pytest's default `/tmp` fixture paths during mount setup. The
repository lockfile needed an update, so the run used an isolated `uv` test
environment and did not modify `uv.lock`. This exercises the worker's
two-invocation source/cache handoff, but not a real Cargo fingerprint hit or
cross-worker-identity compiler reuse.

The code change has not been exercised by the admitted compiler, and no
controlled warm-repeat or edit comparison has been measured. These two
receipts establish retained cache bytes and actual repeated compilation, not
time saved. Do not claim Cargo cache savings until a current-source admitted
run demonstrates that unchanged native dependencies are not recompiled.
