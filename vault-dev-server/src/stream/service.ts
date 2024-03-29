import { genManuallyFedIterator, lruCacheFn } from "@livestack/shared";
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
import {
  ZZDatapointRec,
  ensureStreamRec,
  ensureDatapointRelationRec,
} from "../db/service";
import { Knex } from "knex";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  handlePrimitiveOrArray,
} from "../db/primitives";
import { v4 } from "uuid";
import { validate } from "jsonschema";

export const streamService = (dbConn: Knex): StreamServiceImplementation => {
  const jsonSchemaByStreamId = lruCacheFn(
    ({ projectId, streamId }) => `${projectId}/${streamId}`,
    async ({
      projectId,
      streamId,
    }: {
      projectId: string;
      streamId: string;
    }) => {
      const rec = await dbConn("zz_streams")
        .where("project_id", projectId)
        .andWhere("stream_id", streamId)
        .first<{
          json_schema_str: string;
        }>(["json_schema_str"]);
      if (!rec) {
        throw new Error("Stream not found");
      }
      if (!rec.json_schema_str) {
        return null;
      } else {
        return JSON.parse(rec.json_schema_str) as any;
      }
    }
  );

  const addDatapoint = async ({
    projectId,
    streamId,
    datapointId,
    jobInfo,
    dataStr,
    parentDatapoints,
  }: {
    projectId: string;
    streamId: string;
    datapointId: string;
    jobInfo?: {
      jobId: string;
      outputTag: string;
    };
    dataStr: string;
    parentDatapoints: {
      streamId: string;
      datapointId: string;
    }[];
  }) => {
    await ensureStreamRec(dbConn, {
      project_id: projectId,
      stream_id: streamId,
    });
    await dbConn.transaction(async (trx) => {
      const data = JSON.parse(dataStr);
      await trx<
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

      for (const parentDatapoint of parentDatapoints) {
        await ensureDatapointRelationRec(trx, {
          project_id: projectId,
          source_datapoint_id: parentDatapoint.datapointId,
          source_stream_id: parentDatapoint.streamId,
          target_datapoint_id: datapointId,
          target_stream_id: streamId,
        });
      }
    });

    return { datapointId };
  };

  return {
    ensureStream: async (rec) => {
      return await ensureStreamRec(dbConn, rec);
    },
    async pub(
      request: StreamPubMessage,
      context: CallContext
    ): Promise<{
      success?: { chunkId: string; datapointId: string };
      validationFailure?: {
        errorMessage: string;
        datapointId: string;
      };
    }> {
      const { projectId, streamId, dataStr, jobInfo, parentDatapoints } =
        request;
      const datapointId = v4();
      const channelId = `${projectId}/${streamId}`;
      const pubClient = await createClient().connect();
      // console.debug("pubbing", "to", channelId, "data", dataStr);
      // Publish the message to the Redis stream

      const jsonSchema = await jsonSchemaByStreamId({
        projectId,
        streamId,
      });

      let res = { valid: true, errors: [] as any[] };
      const data = JSON.parse(dataStr) as
        | {
            terminate: true;
          }
        | {
            terminate: false;
            data: any;
          };
      // bypass {terminate: true} layer and validate the just data
      if (jsonSchema && data.terminate === false && jsonSchema["anyOf"]) {
        // sample schema
        // {"anyOf":[{"type":"object","properties":{"data":{"type":"object","properties":{"summarized":{"type":"string"}},"required":["summarized"],"additionalProperties":false},"terminate":{"type":"boolean","const":false}},"required":["data","terminate"],"additionalProperties":false},{"type":"object","properties":{"terminate":{"type":"boolean","const":true}},"required":["terminate"],"additionalProperties":false}],"$schema":"http://json-schema.org/draft-07/schema#"}
        let actualSchema = (jsonSchema["anyOf"] as any[]).find((s: any) => {
          return !!s.properties.data;
        }).properties.data;
        Object.assign(actualSchema, {
          $schema: jsonSchema["$schema"],
        });
        res = validate(data.data, actualSchema, {
          throwError: false,
        });
      }

      const [_, chunkId] = await Promise.all([
        addDatapoint({
          projectId,
          streamId,
          datapointId,
          jobInfo,
          dataStr,
          parentDatapoints,
        }),
        (async () => {
          const chunkId: string = await pubClient.sendCommand([
            "XADD",
            channelId,
            "MAXLEN",
            "~",
            "1000",
            "*",
            "datapointId",
            datapointId,
            "data",
            dataStr,
          ]);
          await pubClient.disconnect();
          return chunkId;
        })(),
      ]);

      if (!res.valid) {
        return {
          validationFailure: {
            errorMessage: res.toString(),
            datapointId,
          },
        };
      }

      return {
        success: {
          chunkId,
          datapointId,
        },
      };
    },
    sub(
      request: SubRequest,
      context: CallContext
    ): ServerStreamingMethodResult<{
      timestamp?: number | undefined;
      chunkId?: string | undefined;
      datapointId?: string | undefined;
      dataStr?: string | undefined;
    }> {
      const { iterator, resolveNext } = genManuallyFedIterator<{
        timestamp: number | undefined;
        chunkId: string | undefined;
        dataStr: string | undefined;
        datapointId: string | undefined;
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
              const { jsonDataStr: dataStr, datapointId } = parseMessageDataStr(
                message[1]
              );
              resolveNext({
                timestamp,
                dataStr,
                chunkId: cursor,
                datapointId,
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
            datapointId?: string | undefined;
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
            const { jsonDataStr: dataStr, datapointId } =
              parseMessageDataStr(messages);
            return {
              datapoint: {
                timestamp,
                dataStr,
                chunkId: message[0],
                datapointId,
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
            datapointId?: string | undefined;
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
            const { jsonDataStr: dataStr, datapointId } =
              parseMessageDataStr(messages);
            return {
              datapoint: {
                timestamp,
                dataStr,
                chunkId: message[0],
                datapointId,
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

function parseMessageDataStr<T>(data: Array<any>): {
  jsonDataStr: string;
  datapointId: string;
} {
  // Look for the 'data' key and its subsequent value in the flattened array
  const dataIdx = data.indexOf("data");
  if (dataIdx === -1 || dataIdx === data.length - 1) {
    console.error("data:", data);
    throw new Error("Data key not found in stream message or is malformed");
  }
  const jsonDataStr = data[dataIdx + 1] as string;

  const datapointIdIdx = data.indexOf("datapointId");
  if (datapointIdIdx === -1 || datapointIdIdx === data.length - 1) {
    console.error("data:", data);
    throw new Error(
      "DatapointId key not found in stream message or is malformed"
    );
  }
  const datapointId = data[datapointIdIdx + 1] as string;

  return { jsonDataStr, datapointId };
}
