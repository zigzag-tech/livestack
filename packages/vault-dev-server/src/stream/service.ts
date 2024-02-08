import { StreamServiceImplementation } from "@livestack/vault-interface";
import {
  StreamPubMessage,
  SubRequest,
  ServerStreamingMethodResult,
} from "@livestack/vault-interface/src/generated/stream";
import { CallContext } from "nice-grpc-common";
import { createClient } from "redis";

class StreamServiceByProject implements StreamServiceImplementation {
  async pub(
    request: StreamPubMessage,
    context: CallContext
  ): Promise<{ messageId: string }> {
    const { projectId, uniqueName, jobId, outputTag, messageIdOverride } =
      request;
    const channelId = `${uniqueName}`;
    const pubClient = await createClient().connect();

    // Publish the message to the Redis stream
    const messageId: string = await pubClient.sendCommand([
      "XADD",
      channelId,
      "*",
      "data",
      request.dataStr,
    ]);

    await pubClient.disconnect();
    return { messageId };
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
