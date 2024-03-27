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
import { ZZDatapointRec, ensureStreamRec } from "../db/service";
import { Knex } from "knex";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  handlePrimitiveOrArray,
} from "../db/primitives";
import { v4 } from "uuid";

export const streamService = (dbConn: Knex): StreamServiceImplementation => {
  const addDatapoint = async ({
    projectId,
    streamId,
    datapointId,
    jobInfo,
    dataStr,
  }: {
    projectId: string;
    streamId: string;
    datapointId: string;
    jobInfo?: {
      jobId: string;
      outputTag: string;
    };
    dataStr: string;
  }) => {
    await ensureStreamRec(dbConn, {
      project_id: projectId,
      stream_id: streamId,
    });
    const data = JSON.parse(dataStr);
    await dbConn<
      ZZDatapointRec<
        | any
        | {
            [PRIMTIVE_KEY]: any;
          }
        | {
            [ARRAY_KEY]: any;
          }
      >
    >("zz_datapoints")
      .insert({
        project_id: projectId,
        stream_id: streamId,
        datapoint_id: datapointId,
        data: handlePrimitiveOrArray(data),
        job_id: jobInfo?.jobId || null,
        job_output_key: jobInfo?.outputTag || null,
        connector_type: jobInfo ? "out" : null,
        time_created: new Date(),
      })
      .onConflict(["project_id", "stream_id", "datapoint_id"])
      .ignore();

    return { datapointId };
  };

  return {
    async pub(
      request: StreamPubMessage,
      context: CallContext
    ): Promise<{ chunkId: string; datapointId: string }> {
      const { projectId, streamId, dataStr, jobInfo } = request;
      const datapointId = v4();
      const channelId = `${projectId}/${streamId}`;
      const pubClient = await createClient().connect();
      // console.debug("pubbing", "to", channelId, "data", dataStr);
      // Publish the message to the Redis stream

      const [_, chunkId] = await Promise.all([
        addDatapoint({
          projectId,
          streamId,
          datapointId,
          jobInfo,
          dataStr,
        }),
        (async () => {
          const chunkId: string = await pubClient.sendCommand([
            "XADD",
            channelId,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "data",
            dataStr,
          ]);
          await pubClient.disconnect();
          return chunkId;
        })(),
      ]);

      return { chunkId, datapointId };
    },
    sub(
      request: SubRequest,
      context: CallContext
    ): ServerStreamingMethodResult<{
      timestamp?: number | undefined;
      chunkId?: string | undefined;
      dataStr?: string | undefined;
    }> {
      const { iterator, resolveNext } = genManuallyFedIterator<{
        timestamp: number | undefined;
        chunkId: string | undefined;
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
                chunkId: cursor,
              });
            }
          }
        }
      })();
      return iterator;
    },

    async lastValue(
      request: LastValueRequest,
      context: CallContext
    ): Promise<{
      datapoint?:
        | {
            timestamp?: number | undefined;
            chunkId?: string | undefined;
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
                chunkId: message[0],
              },
            };
          }
        }
      } catch (e) {
        console.error(e);
        throw e;
      }
      return { null_response: {} };
    },
    async valueByReverseIndex(
      request: ValueByReverseIndexRequest,
      context: CallContext
    ): Promise<{
      datapoint?:
        | {
            timestamp?: number | undefined;
            chunkId?: string | undefined;
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
                chunkId: message[0],
              },
            };
          }
        }
      } catch (e) {
        console.error(e);
        throw e;
      }
      return { null_response: {} };
    },
  };
};

const _projectServiceMap: Record<string, StreamServiceImplementation> = {};

export function getStreamService(dbConn: Knex) {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = streamService(dbConn);
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
