import { genManuallyFedIterator } from "@livestack/shared";
import { StreamServiceImplementation } from "@livestack/vault-interface";
import {
  StreamPubMessage,
  SubRequest,
  ServerStreamingMethodResult,
  SubType,
  ValueByReverseIndexRequest,
  LastValueRequest,
} from "@livestack/vault-interface/src/generated/stream";
import { CallContext } from "nice-grpc-common";
import { createClient } from "redis";

class StreamServiceByProject implements StreamServiceImplementation {
  async pub(
    request: StreamPubMessage,
    context: CallContext
  ): Promise<{ messageId: string }> {
    const { projectId, uniqueName, dataStr } = request;
    const channelId = `${projectId}/${uniqueName}`;
    const pubClient = await createClient().connect();

    // Publish the message to the Redis stream
    const messageId: string = await pubClient.sendCommand([
      "XADD",
      channelId,
      "*",
      "data",
      dataStr,
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
    const { iterator, resolveNext } = genManuallyFedIterator<{
      timestamp: number | undefined;
      messageId: string | undefined;
      dataStr: string | undefined;
    }>();

    (async () => {
      const { projectId, uniqueName, subType } = request;
      let cursor: `${string}-${string}` | "$" | "0" =
        subType === SubType.fromNow ? "$" : "0";
      const channelId = `${projectId}/${uniqueName}`;
      const subClient = await createClient().connect();

      while (context.signal.aborted === false) {
        const stream = (await subClient.sendCommand([
          "XREAD",
          "COUNT",
          "1",
          "BLOCK",
          "1000", // Set a timeout for blocking, e.g., 1000 milliseconds
          "STREAMS",
          channelId,
          cursor,
        ])) as [string, [string, string[]][]][];
        if (stream) {
          const [key, messages] = stream[0]; // Assuming single stream

          for (let message of messages) {
            // id format: 1526919030474-55
            // https://redis.io/commands/xadd/
            cursor = message[0] as `${string}-${string}`;
            const [timestampStr, _] = cursor.split("-");
            const timestamp = Number(timestampStr);
            const dataStr = parseMessageDataStr(message[1]);
            resolveNext({
              timestamp,
              dataStr,
              messageId: cursor,
            });
          }
        }
      }
    })();
    return iterator;
  }

  async lastValue(
    request: LastValueRequest,
    context: CallContext
  ): Promise<{
    datapoint?:
      | {
          timestamp?: number | undefined;
          messageId?: string | undefined;
          dataStr?: string | undefined;
        }
      | undefined;
    null_response?: {} | undefined;
  }> {
    const { projectId, uniqueName } = request;
    const channelId = `${projectId}/${uniqueName}`;
    const subClient = await createClient().connect();

    const s = (await subClient.sendCommand([
      "XREVRANGE",
      channelId,
      "+",
      "-",
      "COUNT",
      `1`,
    ])) as [string, ...[string, string][]][];

    try {
      if (s && s.length > 0) {
        const messages = s[0][1]; // Assuming single stream
        const message = s[0][0];
        if (messages.length > 0) {
          const cursor = message[0] as `${string}-${string}`;
          const [timestampStr, _] = cursor.split("-");
          const timestamp = Number(timestampStr);
          const dataStr = parseMessageDataStr(messages);
          return {
            datapoint: {
              timestamp,
              dataStr,
              messageId: message[0],
            },
          };
        }
      }
    } catch (e) {
      console.error(e);
      throw e;
    }
    return { null_response: {} };
  }

  async valueByReverseIndex(
    request: ValueByReverseIndexRequest,
    context: CallContext
  ): Promise<{
    datapoint?:
      | {
          timestamp?: number | undefined;
          messageId?: string | undefined;
          dataStr?: string | undefined;
        }
      | undefined;
    null_response?: {} | undefined;
  }> {
    const { projectId, uniqueName, index = 0 } = request;
    const channelId = `${projectId}/${uniqueName}`;
    const subClient = await createClient().connect();
    const s = (await subClient.sendCommand([
      "XREVRANGE",
      channelId,
      "+",
      "-",
      "COUNT",
      `${index + 1}`,
    ])) as [string, ...[string, string][]][];
    try {
      if (s && s.length > 0) {
        const p = s[index];
        const messages = p[1]; // Assuming single stream
        const message = p[0];
        if (messages.length > 0) {
          const cursor = message[0] as `${string}-${string}`;
          const [timestampStr, _] = cursor.split("-");
          const timestamp = Number(timestampStr);
          const dataStr = parseMessageDataStr(messages);
          return {
            datapoint: {
              timestamp,
              dataStr,
              messageId: message[0],
            },
          };
        }
      }
    } catch (e) {
      console.error(e);
      throw e;
    }
    return { null_response: {} };
  }
}

const _projectServiceMap: Record<string, StreamServiceByProject> = {};

export function getStreamService() {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = new StreamServiceByProject();
  }
  return _projectServiceMap["default"];
}

function parseMessageDataStr<T>(data: Array<any>): string {
  // Look for the 'data' key and its subsequent value in the flattened array
  const dataIdx = data.indexOf("data");
  if (dataIdx === -1 || dataIdx === data.length - 1) {
    console.error("data:", data);
    throw new Error("Data key not found in stream message or is malformed");
  }

  const jsonDataStr = data[dataIdx + 1] as string;

  return jsonDataStr;
}
