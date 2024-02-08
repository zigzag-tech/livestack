/* eslint-disable */
import type { CallContext, CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";

export const protobufPackage = "livestack";

export interface StreamPubMessage {
  projectId: string;
  uniqueName: string;
  jobId?: string | undefined;
  outputTag?: string | undefined;
  messageIdOverride?: string | undefined;
}

export interface StreamPubResult {
  messageId: string;
}

export interface SubParams {
  projectId: string;
  uniqueName: string;
}

export interface StreamDatapoint {
  timestamp: number;
  messageId: string;
  dataStr: string;
}

function createBaseStreamPubMessage(): StreamPubMessage {
  return { projectId: "", uniqueName: "", jobId: undefined, outputTag: undefined, messageIdOverride: undefined };
}

export const StreamPubMessage = {
  encode(message: StreamPubMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.uniqueName !== "") {
      writer.uint32(18).string(message.uniqueName);
    }
    if (message.jobId !== undefined) {
      writer.uint32(26).string(message.jobId);
    }
    if (message.outputTag !== undefined) {
      writer.uint32(34).string(message.outputTag);
    }
    if (message.messageIdOverride !== undefined) {
      writer.uint32(42).string(message.messageIdOverride);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): StreamPubMessage {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseStreamPubMessage();
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

          message.uniqueName = reader.string();
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

          message.outputTag = reader.string();
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.messageIdOverride = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): StreamPubMessage {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      uniqueName: isSet(object.uniqueName) ? globalThis.String(object.uniqueName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : undefined,
      outputTag: isSet(object.outputTag) ? globalThis.String(object.outputTag) : undefined,
      messageIdOverride: isSet(object.messageIdOverride) ? globalThis.String(object.messageIdOverride) : undefined,
    };
  },

  toJSON(message: StreamPubMessage): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.uniqueName !== "") {
      obj.uniqueName = message.uniqueName;
    }
    if (message.jobId !== undefined) {
      obj.jobId = message.jobId;
    }
    if (message.outputTag !== undefined) {
      obj.outputTag = message.outputTag;
    }
    if (message.messageIdOverride !== undefined) {
      obj.messageIdOverride = message.messageIdOverride;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubMessage>): StreamPubMessage {
    return StreamPubMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubMessage>): StreamPubMessage {
    const message = createBaseStreamPubMessage();
    message.projectId = object.projectId ?? "";
    message.uniqueName = object.uniqueName ?? "";
    message.jobId = object.jobId ?? undefined;
    message.outputTag = object.outputTag ?? undefined;
    message.messageIdOverride = object.messageIdOverride ?? undefined;
    return message;
  },
};

function createBaseStreamPubResult(): StreamPubResult {
  return { messageId: "" };
}

export const StreamPubResult = {
  encode(message: StreamPubResult, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.messageId !== "") {
      writer.uint32(10).string(message.messageId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): StreamPubResult {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseStreamPubResult();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.messageId = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): StreamPubResult {
    return { messageId: isSet(object.messageId) ? globalThis.String(object.messageId) : "" };
  },

  toJSON(message: StreamPubResult): unknown {
    const obj: any = {};
    if (message.messageId !== "") {
      obj.messageId = message.messageId;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubResult>): StreamPubResult {
    return StreamPubResult.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubResult>): StreamPubResult {
    const message = createBaseStreamPubResult();
    message.messageId = object.messageId ?? "";
    return message;
  },
};

function createBaseSubParams(): SubParams {
  return { projectId: "", uniqueName: "" };
}

export const SubParams = {
  encode(message: SubParams, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.uniqueName !== "") {
      writer.uint32(18).string(message.uniqueName);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): SubParams {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseSubParams();
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

          message.uniqueName = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): SubParams {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      uniqueName: isSet(object.uniqueName) ? globalThis.String(object.uniqueName) : "",
    };
  },

  toJSON(message: SubParams): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.uniqueName !== "") {
      obj.uniqueName = message.uniqueName;
    }
    return obj;
  },

  create(base?: DeepPartial<SubParams>): SubParams {
    return SubParams.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<SubParams>): SubParams {
    const message = createBaseSubParams();
    message.projectId = object.projectId ?? "";
    message.uniqueName = object.uniqueName ?? "";
    return message;
  },
};

function createBaseStreamDatapoint(): StreamDatapoint {
  return { timestamp: 0, messageId: "", dataStr: "" };
}

export const StreamDatapoint = {
  encode(message: StreamDatapoint, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.timestamp !== 0) {
      writer.uint32(8).int32(message.timestamp);
    }
    if (message.messageId !== "") {
      writer.uint32(18).string(message.messageId);
    }
    if (message.dataStr !== "") {
      writer.uint32(26).string(message.dataStr);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): StreamDatapoint {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseStreamDatapoint();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 8) {
            break;
          }

          message.timestamp = reader.int32();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.messageId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.dataStr = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): StreamDatapoint {
    return {
      timestamp: isSet(object.timestamp) ? globalThis.Number(object.timestamp) : 0,
      messageId: isSet(object.messageId) ? globalThis.String(object.messageId) : "",
      dataStr: isSet(object.dataStr) ? globalThis.String(object.dataStr) : "",
    };
  },

  toJSON(message: StreamDatapoint): unknown {
    const obj: any = {};
    if (message.timestamp !== 0) {
      obj.timestamp = Math.round(message.timestamp);
    }
    if (message.messageId !== "") {
      obj.messageId = message.messageId;
    }
    if (message.dataStr !== "") {
      obj.dataStr = message.dataStr;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamDatapoint>): StreamDatapoint {
    return StreamDatapoint.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamDatapoint>): StreamDatapoint {
    const message = createBaseStreamDatapoint();
    message.timestamp = object.timestamp ?? 0;
    message.messageId = object.messageId ?? "";
    message.dataStr = object.dataStr ?? "";
    return message;
  },
};

export type StreamServiceDefinition = typeof StreamServiceDefinition;
export const StreamServiceDefinition = {
  name: "StreamService",
  fullName: "livestack.StreamService",
  methods: {
    pub: {
      name: "Pub",
      requestType: StreamPubMessage,
      requestStream: false,
      responseType: StreamPubResult,
      responseStream: false,
      options: {},
    },
    sub: {
      name: "Sub",
      requestType: SubParams,
      requestStream: false,
      responseType: StreamDatapoint,
      responseStream: true,
      options: {},
    },
  },
} as const;

export interface StreamServiceImplementation<CallContextExt = {}> {
  pub(request: StreamPubMessage, context: CallContext & CallContextExt): Promise<DeepPartial<StreamPubResult>>;
  sub(
    request: SubParams,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<StreamDatapoint>>;
}

export interface StreamServiceClient<CallOptionsExt = {}> {
  pub(request: DeepPartial<StreamPubMessage>, options?: CallOptions & CallOptionsExt): Promise<StreamPubResult>;
  sub(request: DeepPartial<SubParams>, options?: CallOptions & CallOptionsExt): AsyncIterable<StreamDatapoint>;
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

export type ServerStreamingMethodResult<Response> = { [Symbol.asyncIterator](): AsyncIterator<Response, void> };
