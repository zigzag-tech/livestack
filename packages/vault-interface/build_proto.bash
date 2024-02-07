#!/bin/bash
# PROTOC_GEN_TS_PATH="../../node_modules/.bin/protoc-gen-ts"
# PROTOC_GEN_JS_PATH="../../node_modules/.bin/protoc-gen-js"

OUT_DIR="./generated"

# npx protoc-gen-grpc-ts \
# --js_out="import_style=commonjs,binary:${OUT_DIR}" \
# --grpc_out="grpc_ts:${OUT_DIR}" \
# --proto_path ./proto \
# ./proto/zzcloud.proto

# protoc \
#     -I ./proto \
#     --ts_out="${OUT_DIR}" \
#     ./proto/zzcloud.proto

# https://rsbh.dev/blogs/grpc-with-nodejs-typescript
protoc --plugin=$(npm root)/.bin/protoc-gen-ts_proto \
 --ts_proto_out="${OUT_DIR}" \
 --ts_proto_opt=outputServices=grpc-js \
 --ts_proto_opt=esModuleInterop=true \
 -I=proto/ proto/*.proto