# Harmony policy lab source map

Snapshot date: 2026-09-17. This is an implementation boundary record, not a
claim that consumer instrumentation is enabled.

## Ownership and reuse map

| Area | Authoritative source | Lab use | Ownership boundary |
|---|---|---|---|
| Local placement/residency | `livestack_node/planner.py` (`plan`, `_best_placement`, `_contention_cost`) | pinned incumbent adapter and behavior fixtures | live broker remains executor |
| Host state/demand | `livestack_node/hostbroker.py` (`snapshot`, `_note_demand`, `plan_and_apply`) | metadata observer; decayed demand is not unique global demand | host broker owns preparation and local grants |
| Fleet placement | `livestack_node/fleet_scheduler.py` (`schedule`, `_eta`) and `fleet_admit.py` (`targets_from_view`, `admit`) | incumbent routing adapter; explicitly model missing deep queue/cold-ready semantics | fleet authority keeps live admission |
| Existing evidence | `livestack_node/ledger.py` and `decision.schema.json` | translate facts whose semantics match; never backfill absent facts | old records remain immutable |
| Durable work | `livestack_node/workloads/` (`model.submission`, `placement.place`, input cache/mirror, attempts/artifacts) | future admitted experiment execution and durable artifacts | no second job authority in the lab |
| Generic inference clients | consumer-owned adapter | reusable LLM/embedding metadata hooks | local tools and semantic repair remain client-owned |
| Multi-region speech clients | consumer-owned adapter | ASR/TTS route and coverage observations | account/relay permissions remain consumer-owned |
| Desktop speech clients | consumer-owned adapter | request/audible/fallback observations | engine order, voice and shortcut remain unchanged |
| Workflow/batch clients | consumer-owned adapter | workflow, lease, stage and budget observations | degraded paths are recorded, not repaired by instrumentation |
| Transform/inference clients | consumer-owned adapter | inference/embedding provenance observations | cache and semantic behavior remain unchanged |

The new `livestack_node.policy_lab` package is CPU-only and owns schemas,
offline import, profiles, simulation, evaluation and experiment artifacts. It
must not import live brokers, GPU runtimes, model weights, SSH/HTTP clients, or
the Rust residency binding.

## Rules and checkout state

- Livestack: clean `main` at `b0c2b78f` before this change; no local
  `AGENTS.md`. Only the additive policy-lab package/tests/docs are now untracked.
- Consumer adapters live in separately governed repositories. Their local rules and
  pre-existing work must be inspected and preserved before changes.
- No existing Livestack symbol was edited for M0; new files had no upstream callers.
  Before later adapter edits, use each repository's configured impact tooling or
  explicitly record its unavailability.

## Live-state guarantee

M0 creates offline files only. It does not call admission endpoints, load
models, install services, register timers, alter routes, or write consumer
configuration.
