## 1. Phase A: liveness (no new host)
- [ ] 1.1 Specify the stall deadline, throughput-scaled ceiling and adaptive block rule once, with a table-driven test fixture both languages run. Test: fixture cases (fast, slow, stalled, trickle). Ledger: n/a, no placement decision.
- [ ] 1.2 livestack `download.py` / `block_codec.py` / `route_kinds.py`: stall-based abandonment and adaptive range size. Tests in `test_workload_routes.py` with a real local server injecting stalls and slow bodies. Ledger: route trail records chosen block size and stall events.
- [ ] 1.3 zzops `harmony-response.mjs`, `harmony-download.mjs`: replace the fixed 60 s timer with the stall rule; ranged, resumable, digest-verified download. Tests: private fixture server (no production authority).
- [ ] 1.4 Startup and trail reporting: `single_route`, `degraded_path: mesh_derp`. Tests: config with one and two doors. Ledger: reason recorded in the trail.

## 2. Phase B: doors as data
- [ ] 2.1 Worker config `object_relay` accepts a list of doors; schema-validated, object form still valid. Test: both forms parse; unknown fields refused.
- [ ] 2.2 Per-door tunnel unit template (benchday `ops/harmony-edge`, zzops `edge-units.py`): independent failure per door. Test: one tunnel killed in the existing systemd rehearsal, others unaffected.
- [ ] 2.3 Per-principal grant base URL and zzops `sourceUploadOrigin` accept a list or fall back to the authority; iOS worker's relay allowlist reads the door list. Tests: grant origin binding with two origins.
- [ ] 2.4 Commission a second door off the control-plane host (operator step, after the open questions are answered; includes a measured 100 MB sha256-verified transfer from Nanjing).

## 3. Phase C: meshlink transport
- [ ] 3.1 `mesh` route kind delegating to `mesh-route-py` (transfer-routes 4.4). Test: real mesh relay harness.
- [ ] 3.2 Authority attaches to a relay door as a `mesh_outbound` target; SSH `-R` stays as a transport. Test: attach, rotate key, relay restart does not drop in-flight resumes.

## 4. Verification and rollout (operator-run, separate)
- [ ] 4.1 Fault drill on the real path with one door blocked, then all doors blocked (loud `single_route` and trail, authority fallback completes at the saved offset).
- [ ] 4.2 `openspec validate --specs` green; archive when shipped.
