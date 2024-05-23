import type {
  AllValuesRequest,
  ServerStreamingMethodResult,
  StreamPubMessage,
  SubRequest,
  ValuesByReverseIndexRequest,
  LastValueRequest,
} from "@livestack/vault-interface//src/generated/stream.js";
import sharedPkg from "@livestack/shared";
const { OBJ_REF_VALUE, genManuallyFedIterator, lruCacheFn } = sharedPkg;
import { StreamServiceImplementation } from "@livestack/vault-interface";
import streamPkg from "@livestack/vault-interface/src/generated/stream.js";
const { SubType } = streamPkg;
import { CallContext } from "nice-grpc-common";
import { createClient } from "redis";
import {
  ZZDatapointRec,
  ensureStreamRec,
  ensureDatapointRelationRec,
} from "../db/service.js";
import { Knex } from "knex";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  handlePrimitiveOrArray,
} from "../db/primitives.js";
import { v4 } from "uuid";
// import { validate } from "jsonschema";
import Ajv from "ajv";
import _ from "lodash";
import { Readable } from "stream";

export const streamService = (dbConn: Knex): StreamServiceImplementation => {
  const jsonSchemaByStreamId = lruCacheFn(
    ({ projectUuid, streamId }) => `${projectUuid}/${streamId}`,
    async ({
      projectUuid,
      streamId,
    }: {
      projectUuid: string;
      streamId: string;
    }) => {
      const rec = await dbConn("zz_streams")
        .where("project_uuid", projectUuid)
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
    projectUuid,
    streamId,
    datapointId,
    jobInfo,
    dataStr,
    parentDatapoints,
  }: {
    projectUuid: string;
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
      project_uuid: projectUuid,
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
        project_uuid: projectUuid,
        stream_id: streamId,
        datapoint_id: datapointId,
        data: handlePrimitiveOrArray(data),
        job_id: jobInfo?.jobId || null,
        job_output_key: jobInfo?.outputTag || null,
        connector_type: jobInfo ? "out" : null,
        time_created: new Date(),
      })
      .onConflict(["project_uuid", "stream_id", "datapoint_id"])
      .ignore();

    for (const parentDatapoint of parentDatapoints) {
      await ensureDatapointRelationRec(dbConn, {
        project_uuid: projectUuid,
        source_datapoint_id: parentDatapoint.datapointId,
        source_stream_id: parentDatapoint.streamId,
        target_datapoint_id: datapointId,
        target_stream_id: streamId,
      });
    }

    return { datapointId };
  };

  const addValidationResultRec = async ({
    projectUuid,
    streamId,
    datapointId,
    validationResult,
  }: {
    projectUuid: string;
    streamId: string;
    datapointId: string;
    validationResult: {
      valid: boolean;
      errors: any[];
    };
  }) => {
    await dbConn("zz_data_validation_results")
      .insert({
        project_uuid: projectUuid,
        stream_id: streamId,
        datapoint_id: datapointId,
        validation_failed: !validationResult.valid,
      })
      .onConflict(["project_uuid", "stream_id", "datapoint_id"])
      .ignore();
  };

  const processRawDatapoint = (p: [string, ...[string, string][]]) => {
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
    } else {
      return { null_response: {} };
    }
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
      const { projectUuid, streamId, dataStr, jobInfo, parentDatapoints } =
        request;
      const datapointId = v4();
      const channelId = `${projectUuid}/${streamId}`;
      // console.debug("pubbing", "to", channelId, "data", dataStr);
      // Publish the message to the Redis stream

      const jsonSchema = await jsonSchemaByStreamId({
        projectUuid,
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
        try {
          // res = validate(data.data, actualSchema, {
          //   throwError: false,
          // });

          const ajv = new Ajv({
            useDefaults: true,
            coerceTypes: true,
            removeAdditional: false,
          });


          const valid = ajv.validate(jsonSchema, replaceRefWithDummyData(data));
          if (!valid) {
            res = {
              valid: false,
              errors: (ajv.errors || []).map((e) => {
                return {
                  message: e.message,
                  dataPath: e.dataPath,
                  schemaPath: e.schemaPath,
                  keyword: e.keyword,
                  params: e.params,
                };
              }),
            };
          }
        } catch (e) {
          console.warn("Error occurred validating data: ", e);
          console.debug("actualSchema: ", JSON.stringify(actualSchema));
          console.debug("data: ", JSON.stringify(data.data));
          res = {
            valid: false,
            errors: [
              {
                message: "Error occurred validating data",
              },
            ],
          };
        }
      }

      await addDatapoint({
        projectUuid,
        streamId,
        datapointId,
        jobInfo,
        dataStr,
        parentDatapoints,
      });

      let chunkId: string;
      const pubClient = await createClient().connect();

      if (res.valid) {
        try {
          chunkId = await pubClient.sendCommand([
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
        } catch (e) {
          throw e;
        } finally {
          await pubClient.disconnect();
        }
        return {
          success: {
            chunkId,
            datapointId,
          },
        };
      }
      // not valid
      else {
        await addValidationResultRec({
          projectUuid,
          streamId,
          datapointId,
          validationResult: res,
        });
        return {
          validationFailure: {
            errorMessage: res.toString(),
            datapointId,
          },
        };
      }
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
        const { projectUuid, uniqueName, subType } = request;
        let cursor: `${string}-${string}` | "$" | "0" =
          subType === SubType.fromNow ? "$" : "0";
        const channelId = `${projectUuid}/${uniqueName}`;
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
      const { projectUuid, uniqueName } = request;
      const channelId = `${projectUuid}/${uniqueName}`;
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
    async allValues(
      request: AllValuesRequest,
      context: CallContext
    ): Promise<{
      datapoints: {
        datapoint?:
          | {
              timestamp?: number | undefined;
              chunkId?: string | undefined;
              dataStr?: string | undefined;
              datapointId?: string | undefined;
            }
          | undefined;
        null_response?: {} | undefined;
      }[];
    }> {
      const { projectUuid, uniqueName } = request;
      const channelId = `${projectUuid}/${uniqueName}`;
      const subClient = await createClient().connect();
      // get all values from the stream

      const s = (await subClient.sendCommand([
        "XRANGE",
        channelId,
        "-",
        "+",
      ])) as [string, ...[string, string][]][];

      try {
        if (s && s.length > 0) {
          return {
            datapoints: s.map(processRawDatapoint),
          };
        }
      } catch (e) {
        console.error(e);
        throw e;
      }
      return {
        datapoints: [],
      };
    },
    async valuesByReverseIndex(
      request: ValuesByReverseIndexRequest,
      context: CallContext
    ): Promise<{
      datapoints: {
        datapoint?:
          | {
              timestamp?: number | undefined;
              chunkId?: string | undefined;
              dataStr?: string | undefined;
              datapointId?: string | undefined;
            }
          | undefined;
        null_response?: {} | undefined;
      }[];
    }> {
      const { projectUuid, uniqueName, lastN } = request;
      const channelId = `${projectUuid}/${uniqueName}`;
      const subClient = await createClient().connect();
      const s = (await subClient.sendCommand([
        "XREVRANGE",
        channelId,
        "+",
        "-",
        "COUNT",
        `${lastN}`,
      ])) as [string, ...[string, string][]][];
      try {
        if (s && s.length > 0) {
          return {
            datapoints: s.map(processRawDatapoint),
          };
        }
      } catch (e) {
        console.error(e);
        throw e;
      }
      return {
        datapoints: [],
      };
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

// resursively replace objects that has [OBJ_REF_VALUE] with dummy data
// clone the object before modifying
function replaceRefWithDummyData(obj: any): any {
  if (typeof obj === "object" && obj !== null && !Array.isArray(obj)) {
    if (obj[OBJ_REF_VALUE]) {
      switch (obj.originalType) {
        case "array-buffer":
          return new ArrayBuffer(1);
        case "stream":
          return new Readable();
        case "blob":
          return new Blob();
        case "file":
          return new File([], "dummy");
        case "buffer":
          return Buffer.from([]);
        case "string":
          return "";
        default:
          throw new Error(`Unknown originalType: ${obj.originalType}`);
      }
    } else {
      const newObj = { ...obj };
      for (const key in obj) {
        if (obj.hasOwnProperty(key)) {
          newObj[key] = replaceRefWithDummyData(obj[key]);
        }
      }
      return newObj;
    }
  } else if (Array.isArray(obj)) {
    return obj.map((item) => replaceRefWithDummyData(item));
  } else {
    return obj;
  }
}
