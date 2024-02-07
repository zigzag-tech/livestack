/* eslint-disable */
import type { CallContext, CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";
import { Empty } from "./google/protobuf/empty";

export const protobufPackage = "livestack";

export interface QueueJob {
  projectId: string;
  specName: string;
  jobId: string;
  contextId: string;
  jobOptionsStr: string;
}

function createBaseQueueJob(): QueueJob {
  return { projectId: "", specName: "", jobId: "", contextId: "", jobOptionsStr: "" };
}

export const QueueJob = {
  encode(message: QueueJob, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.contextId !== "") {
      writer.uint32(34).string(message.contextId);
    }
    if (message.jobOptionsStr !== "") {
      writer.uint32(42).string(message.jobOptionsStr);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): QueueJob {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseQueueJob();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.projectId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.jobId = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.contextId = reader.string();
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.jobOptionsStr = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): QueueJob {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      contextId: isSet(object.contextId) ? globalThis.String(object.contextId) : "",
      jobOptionsStr: isSet(object.jobOptionsStr) ? globalThis.String(object.jobOptionsStr) : "",
    };
  },

  toJSON(message: QueueJob): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.contextId !== "") {
      obj.contextId = message.contextId;
    }
    if (message.jobOptionsStr !== "") {
      obj.jobOptionsStr = message.jobOptionsStr;
    }
    return obj;
  },

  create(base?: DeepPartial<QueueJob>): QueueJob {
    return QueueJob.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<QueueJob>): QueueJob {
    const message = createBaseQueueJob();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    message.jobId = object.jobId ?? "";
    message.contextId = object.contextId ?? "";
    message.jobOptionsStr = object.jobOptionsStr ?? "";
    return message;
  },
};

export type QueueServiceDefinition = typeof QueueServiceDefinition;
export const QueueServiceDefinition = {
  name: "QueueService",
  fullName: "livestack.QueueService",
  methods: {
    addJob: {
      name: "AddJob",
      requestType: QueueJob,
      requestStream: false,
      responseType: Empty,
      responseStream: false,
      options: {},
    },
  },
} as const;

export interface QueueServiceImplementation<CallContextExt = {}> {
  addJob(request: QueueJob, context: CallContext & CallContextExt): Promise<DeepPartial<Empty>>;
}

export interface QueueServiceClient<CallOptionsExt = {}> {
  addJob(request: DeepPartial<QueueJob>, options?: CallOptions & CallOptionsExt): Promise<Empty>;
}

type Builtin = Date | Function | Uint8Array | string | number | boolean | undefined;

export type DeepPartial<T> = T extends Builtin ? T
  : T extends globalThis.Array<infer U> ? globalThis.Array<DeepPartial<U>>
  : T extends ReadonlyArray<infer U> ? ReadonlyArray<DeepPartial<U>>
  : T extends {} ? { [K in keyof T]?: DeepPartial<T[K]> }
  : Partial<T>;

function isSet(value: any): boolean {
  return value !== null && value !== undefined;
}
