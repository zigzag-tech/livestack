#!/bin/bash

OUT_DIR="./src/generated"

protoc --plugin=$(npm root)/.bin/protoc-gen-ts_proto \
 --ts_proto_out="${OUT_DIR}" \
 --ts_proto_opt=outputServices=default \
 --ts_proto_opt=esModuleInterop=true \
 -I=src/ src/*.proto