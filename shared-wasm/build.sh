#!/bin/bash
# stop if any command fails
set -e

# install wasm-pack if not already installed
if ! command -v wasm-pack &> /dev/null; then
    curl https://rustwasm.github.io/wasm-pack/installer/init.sh -sSf | sh
fi

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