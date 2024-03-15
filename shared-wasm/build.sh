#!/bin/bash


# cd into the directory where the script is
cd "$(dirname "$0")"

# ensure pkg/src exists
mkdir -p ./build_tmp/dist

wasm-pack build --out-dir ./build_tmp --out-name dist/livestack_shared_wasm --target nodejs
# jq '.name = "@livestack/shared-wasm"' ../shared-wasm-pkg/package.json > tmp.$$.json && mv tmp.$$.json ../shared-wasm-pkg/package.json
rm ./build_tmp/.gitignore

rsync -av ./build_tmp/ ./ --remove-source-files
rm -rf ./build_tmp