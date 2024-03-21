#!/bin/bash


# cd into the directory where the script is
cd "$(dirname "$0")"

# ensure pkg/src exists
mkdir -p ./build_tmp/
wasm-pack build --out-dir ./build_tmp --out-name livestack_shared_wasm --target web
# jq '.name = "@livestack/shared-wasm"' ../shared-wasm-pkg/package.json > tmp.$$.json && mv tmp.$$.json ../shared-wasm-pkg/package.json
rm ./build_tmp/.gitignore

rsync -av ./build_tmp/ ../shared/src/graph/wasm --remove-source-files
rm -rf ./build_tmp


# ensure pkg/src exists
mkdir -p ./build_tmp_nodejs/
wasm-pack build --out-dir ./build_tmp_nodejs --out-name livestack_shared_wasm_nodejs --target nodejs
jq '.name = "@livestack/shared-wasm"' ../shared-wasm/package.json > tmp.$$.json && mv tmp.$$.json ../shared-wasm/package.json
rm ./build_tmp_nodejs/.gitignore

rsync -av ./build_tmp_nodejs/ ../shared/src/graph/wasm --remove-source-files
rm -rf ./build_tmp_nodejs