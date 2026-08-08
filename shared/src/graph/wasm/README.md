# wasm (generated)

This directory contains generated `wasm-pack` build artifacts. Do not edit by hand.

Produced by `shared-wasm/build.sh`, which runs `wasm-pack build` from the `shared-wasm` crate twice:

- `--target web` → `livestack_shared_wasm.js` / `.wasm` / `.d.ts`
- `--target nodejs` → `livestack_shared_wasm_nodejs.js` / `.wasm` / `.d.ts`

Consumers:

- `@livestack/shared` copies this directory into `dist/wasm` during build (`shared/package.json` `build` script).
- `shared/src/graph/DefGraph.ts` and `shared/src/graph/InstantiatedGraph.ts` import/require from it.
