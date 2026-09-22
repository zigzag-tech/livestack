## @livestack/shared

Shared core Livestack logic for both frontend and backend.

Two layers:

- TypeScript graph/stream spec layer: `IOSpec.ts`, `StreamDefSet.ts`, `graph/DefGraph.ts`, `graph/InstantiatedGraph.ts`.
- Rust core (`src/lib.rs`, `residency.rs`, `models.rs`, `systems/`) compiled to wasm and called from the TS graph code. The wasm artifacts are built by `@livestack/shared-wasm` and vendored into `src/graph/wasm/`.

Endpoint-selection moved to its product owner, private Meshlink, in 0.0.33.
Livestack retains graph, residency, and workload authority and has no Meshlink
dependency. The retired route API is intentionally not retained as a fallback.

Consumed by `core`, `gateway`, `client`, `lab-server`, `vault-dev-server`, and `summarizer`.

Build: `npm run build` (cargo build + preconstruct, then copies `src/graph/wasm` into `dist/wasm`).

Test: `npm test` (jest) and `cargo test` (`tests/def_graph_tests.rs`, `tests/instantiated_graph_test.rs`).
