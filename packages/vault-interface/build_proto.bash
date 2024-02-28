#!/bin/bash
# https://rsbh.dev/blogs/grpc-with-nodejs-typescript

OUT_DIR="./src/generated"

protoc --plugin=$(npm root)/.bin/protoc-gen-ts_proto \
 --ts_proto_out="${OUT_DIR}" \
 --ts_proto_opt=outputServices=nice-grpc,outputServices=generic-definitions,useExactTypes=false \
 --ts_proto_opt=esModuleInterop=true \
 --ts_proto_opt=snakeToCamel=false \
 --ts_proto_opt=useAbortSignal=true \
 --ts_proto_opt=addGrpcMetadata=true \
 -I=src/ src/*.proto