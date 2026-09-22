# livestack-shared-wasm

wasm-bindgen bindings exposing types from the `livestack-shared` Rust crate to
JavaScript/TypeScript: `DefGraph` and instantiated-graph types
(`src/def_graph_wasm.rs`, `src/instantiated_graph_wasm.rs`).

Published to npm as `@livestack/shared-wasm` (v0.0.33). Route-selection
exports were removed in 0.0.33 and now belong to private Meshlink.

## Build

```
./build.sh
```

The script runs `wasm-pack build` twice — once with `--target web`
(`--out-name livestack_shared_wasm`) and once with `--target nodejs`
(`--out-name livestack_shared_wasm_nodejs`) — then rsyncs the artifacts into
`../shared/src/graph/wasm`, where the `@livestack/shared` package consumes
them.

## Dependencies

- `livestack-shared` (path `../shared`)
- `wasm-bindgen`, `js-sys`, `serde`, `serde-wasm-bindgen`, `tsify`
- `console_error_panic_hook` (optional, enabled by default)

## License

Licensed under either of

* Apache License, Version 2.0, ([LICENSE_APACHE](LICENSE_APACHE) or http://www.apache.org/licenses/LICENSE-2.0)
* MIT license ([LICENSE_MIT](LICENSE_MIT) or http://opensource.org/licenses/MIT)

at your option.
