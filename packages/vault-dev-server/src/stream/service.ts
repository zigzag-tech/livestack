import { StreamServiceImplementation } from "@livestack/vault-interface";
import {
  StreamPubMessage,
  SubRequest,
  ServerStreamingMethodResult,
} from "@livestack/vault-interface/src/generated/stream";
import { CallContext } from "nice-grpc-common";
import { createClient } from "redis";

class StreamServiceByProject implements StreamServiceImplementation {
  pub(
    request: StreamPubMessage,
    context: CallContext
  ): Promise<{ messageId?: string | undefined }> {
    throw new Error("Method not implemented.");
  }
  sub(
    request: SubRequest,
    context: CallContext
  ): ServerStreamingMethodResult<{
    timestamp?: number | undefined;
    messageId?: string | undefined;
    dataStr?: string | undefined;
  }> {
    throw new Error("Method not implemented.");
  }
}

const _projectServiceMap: Record<string, StreamServiceByProject> = {};

export function getStreamService() {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = new StreamServiceByProject();
  }
  return _projectServiceMap["default"];
}
