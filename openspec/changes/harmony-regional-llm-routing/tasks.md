## 1. Phase 1 — one requirement evaluator

- [ ] 1.1 Move `attrs_meet` into `livestack_node` (one module), add `{"not": v}` / `{"not": [..]}` with "undeclared satisfies nothing"; make the facade, `fleet_rank` and the planner's `_unit_satisfies` call it. Verify: new cases in `tests/test_planner.py` and `tests/test_harmony_llm_routing.py` (negation excludes; undeclared fails negation) pass, and the existing node-py suite stays green. Ledger: none (no decision changes shape).

## 2. Phase 1 — region before readiness, caller vantage

- [ ] 2.1 Add `allow_regions` to `fleet_rank.rank()`, filtering before warm/cold, finish-time and preference ordering; filtered rows name the region clause. Verify: `tests/test_region_policy.py` gains "warm forbidden `na` does not suppress cold allowed `cn`" and "unknown region excluded"; the existing "no restriction ⇒ identical ranking" invariant still passes. Ledger: filtered rows appear in the ranking record.
- [ ] 2.2 `/fleet/rank` and `/fleet/admit` pass `regions=` into `rank()` instead of post-filtering. Verify: `tests/test_fleet_admit_regions.py` and `tests/test_rank_region_vantage.py` pass unchanged plus one new case for the warm-forbidden scenario through HTTP. Ledger: `/fleet/rank` rows unchanged in shape.
- [ ] 2.3 `fleet_route.choose()` ranks with `vantage=region:<LIVESTACK_NODE_REGION>` and the request's allowed regions; the route record names vantage and regions. Verify: `tests/test_harmony_llm_routing.py` case "cn entry, na broker ⇒ cn target first". Ledger: `fleet_route` record gains `vantage`, `regions`.

## 3. Phase 1 — routed-request contract

- [ ] 3.1 Routing sets `x-harmony-routed: <host>;expect=warm|cold` (cold when the chosen target was cold in the ranking). Verify: unit test on the header for both cases. Ledger: `fleet_route.expect`.
- [ ] 3.2 Receiver of `expect=warm`: resident + satisfies + slot acquired as one step, unit pinned against eviction while held; else `409 {"error":"route_stale","reason":…}` before any body byte; never admit/load/forward. Missing `expect` ⇒ cold (today). Verify: `tests/test_harmony_llm_routing.py` cases not_resident (no load started), unsatisfied, saturated (not queued); `tests/test_harmony_llm_shutdown_during_load.py` still green. Ledger: target demand record outcome `route_stale` with reason.
- [ ] 3.3 Router retries once on `route_stale`, pre-byte connection failure, or 429; drops a `not_resident` target from its cached view until refresh; never retries after a body byte; failure response carries both attempts. Verify: tests for one-retry-then-explained-failure and no-replay-after-first-chunk (streaming). Ledger: route record lists each attempt and refusal.
- [ ] 3.4 Same-host peer forward travels as asked (no `model` rewrite, `harmony_requires` kept), matching fleet forwards. Verify: test asserting the peer receives the original requirement. Ledger: none.
- [ ] 3.5 Request path peer shortcut: same host only, attributes satisfy, resident not loading; warm-on-start keeps "resident or loading". Verify: test with a remote-host holder ⇒ zero `/health` probes on the request path; `tests/test_harmony_llm_loop_blocking.py` green. Ledger: `shortcut` field names "same-host peer" when used.
- [ ] 3.6 `x-request-id` propagated unchanged (minted by the first routing node when absent) and written to both route and demand records. Verify: test joining router and target records by id. Ledger: both records carry `request_id`.

## 4. Phase 1 — verification on the na fleet

- [ ] 4.1 Redeploy harmony-llm and fleetd on xc-tower-ubuntu and zz-whitby together; `/harmony/route` dry runs show vantage, regions and expectation. Verify: dry-run output pasted into HARMONY.md's new section.
- [ ] 4.2 Forced stale route: evict the tower's 27B, send a warm-routed request to it ⇒ 409 `route_stale/not_resident`, retry served by whitby or a refusal naming both. Verify: demand and route records joined by request id.

## 5. Phase 2 — regional routing

- [ ] 5.1 Widen the routing trigger: a request with no resident, unsaturated, in-policy local unit is ranked over the view before any local admission. Verify: test "indifferent request, nothing local, warm in-region peer ⇒ routed warm, no local load"; existing local-first tests green. Ledger: route record `trigger: no_warm_local | speed`.
- [ ] 5.2 Router mode: zero units ⇒ every request routed; `/v1` and `/harmony/route` served. Verify: test with an empty units file routing to a fake target byte-for-byte (stream and non-stream). Ledger: route record on the router.
- [ ] 5.3 Principal region allowance in the fleet principal table; `harmony_regions` may narrow, widening ⇒ 403 naming region and allowance; no allowance ⇒ unrestricted. Verify: `tests/test_harmony_llm_owner.py` gains narrow/widen cases. Ledger: route record `regions` and `regions_source`.
- [ ] 5.4 Bounded stale view: refresh on TTL; on fleetd failure keep the last view up to `HARMONY_LLM_VIEW_STALE_MAX_S` (default 300); past it, local and same-host only. Verify: tests for within-bound routing (record shows `view_age_s`) and beyond-bound fallback. Ledger: `view_age_s`, `view_stale` on route records.
- [ ] 5.5 Deploy a `cn` fleetd on zz-tower2; set `HARMONY_LLM_FLEET_URL` and `LIVESTACK_NODE_REGION=cn` on cn nodes. Verify: `curl <cn fleetd>/fleet` lists every node with a region.

## 6. Router deployment

- [ ] 6.1 Router mode skips `attach()`/announce entirely and serves a local `/health` with version, region and fleet URL. Verify: test that a zero-unit node makes no announce/register call, and a fleetd fixture's `/fleet` never lists it. Ledger: none.
- [ ] 6.2 Router forwards caller `Authorization`/owner headers unchanged and attaches no token even when `HARMONY_LLM_FLEET_TOKEN` is set. Verify: test with and without caller credentials. Ledger: target demand record `principal` reflects the caller only.
- [ ] 6.3 Routing contract version on routed requests (`x-harmony-routed: …;v=<n>`); a target that does not support it answers 409 naming both versions. Verify: skew test in `tests/test_harmony_llm_routing.py`. Ledger: route record `contract_version`.
- [ ] 6.4 Commit `node-py/examples/harmony-llm/systemd/router/harmony-router.service` (systemd user unit) and `io.zztech.harmony-router.plist` (launchd), env file template, loopback bind. Verify: `systemd-analyze --user verify` and `plutil -lint` pass.
- [ ] 6.5 `scripts/harmony-router-rollout.sh [host…] [--status]`: pinned revision from the GPU nodes' `/health`, dedicated worktree, venv, env + unit install, linger, start, post-install `/harmony/route` dry run that must name a target; idempotent. Verify: run twice against lappy — second run reports no changes; `--status` prints version/region/dry run.
- [ ] 6.6 zz-tower2 router as the `cn` ingress (also binds its mesh IP). Verify: a dry run from zz-tower0 via `100.64.0.12:8188` names a `cn` target; route record shows the cn vantage.
- [ ] 6.7 Roll out to zz-tower0, xc-mac-studio, lappy, xc-win-1 WSL. Verify: `--status` green on every host; each host's dry run names a target in its own region.
- [ ] 6.8 Cut callers over, one change each in its own repo: benchday hub `llmConfigBridge.ts` + `narration/summarize.ts`, media-corpus ingest, jingway catalog route — `base_url` becomes `http://127.0.0.1:8188/v1`. Verify per caller: its demand records arrive at a target carrying a router id; no caller config still names `100.64.0.18:8188`.

## 7. Docs

- [ ] 7.1 HARMONY.md: routing section (eligibility, vantage, routed contract, router mode and its deployment/rollout runbook, stale views, non-goals incl. fairness); remove "Only a SPEED preference routes across hosts". Update `_plans/fleet-broker.md` (one broker per region) and `_plans/region-and-locality.md` (filter before readiness). Verify: doc review; links resolve.
- [ ] 7.2 benchday `docs/livestack-harmony.md`: caller-facing note — point `base_url` at the regional ingress or a localhost router; `harmony_regions`; `route_stale`. Verify: landed in benchday via its `scripts/land.sh`.
