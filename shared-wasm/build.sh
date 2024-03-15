#!/bin/bash

wasm-pack build --out-dir ../shared-wasm-pkg --target nodejs
# jq '.name = "@livestack/shared-wasm"' ../shared-wasm-pkg/package.json > tmp.$$.json && mv tmp.$$.json ../shared-wasm-pkg/package.json