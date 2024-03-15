#!/bin/bash

cd shared-wasm
sh ./build.sh

cd ../shared-ts
yarn build