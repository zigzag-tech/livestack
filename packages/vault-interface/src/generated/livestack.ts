/* eslint-disable */
import {
  ChannelCredentials,
  Client,
  ClientReadableStream,
  handleServerStreamingCall,
  makeGenericClientConstructor,
  Metadata,
} from "@grpc/grpc-js";
import type {
  CallOptions,
  ClientOptions,
  ClientUnaryCall,
  handleUnaryCall,
  ServiceError,
  UntypedServiceImplementation,
} from "@grpc/grpc-js";
import _m0 from "protobufjs/minimal";
import { Any } from "./google/protobuf/any";
import { Empty } from "./google/protobuf/empty";
import { Struct } from "./google/protobuf/struct";
import { Timestamp } from "./google/protobuf/timestamp";

export const protobufPackage = "livestack";

export interface GetZZJobTestRequest {
  id: string;
}

export interface GetZZJobTestResponse {
  projectId: string;
  pipeName: string;
  jobId: string;
}

export interface EnsureStreamRecRequest {
  projectId: string;
  streamId: string;
}

export interface EnsureJobStreamConnectorRecRequest {
  projectId: string;
  streamId: string;
  jobId: string;
  key: string;
  connectorType: string;
}

export interface GetJobStreamConnectorRecsRequest {
  projectId: string;
  jobId: string;
  key: string;
  connectorType: string;
}

export interface JobStreamConnectorRecord {
  projectId: string;
  jobId: string;
  timeCreated: Date | undefined;
  streamId: string;
  key: string;
  connectorType: string;
}

export interface GetJobDatapointsRequest {
  projectId: string;
  pipeName: string;
  jobId: string;
  ioType: string;
  order: string;
  limit: number;
  key: string;
}

export interface DatapointRecord {
  datapointId: string;
  data: { [key: string]: any } | undefined;
}

export interface JobInfo {
  jobId: string;
  jobOutputKey: string;
}

export interface AddDatapointRequest {
  projectId: string;
  streamId: string;
  datapointId: string;
  jobInfo: JobInfo | undefined;
  data: Any | undefined;
}

export interface AddDatapointResponse {
  datapointId: string;
}

function createBaseGetZZJobTestRequest(): GetZZJobTestRequest {
  return { id: "" };
}

export const GetZZJobTestRequest = {
  encode(message: GetZZJobTestRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.id !== "") {
      writer.uint32(10).string(message.id);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): GetZZJobTestRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetZZJobTestRequest();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.id = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): GetZZJobTestRequest {
    return { id: isSet(object.id) ? globalThis.String(object.id) : "" };
  },

  toJSON(message: GetZZJobTestRequest): unknown {
    const obj: any = {};
    if (message.id !== "") {
      obj.id = message.id;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetZZJobTestRequest>, I>>(base?: I): GetZZJobTestRequest {
    return GetZZJobTestRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetZZJobTestRequest>, I>>(object: I): GetZZJobTestRequest {
    const message = createBaseGetZZJobTestRequest();
    message.id = object.id ?? "";
    return message;
  },
};

function createBaseGetZZJobTestResponse(): GetZZJobTestResponse {
  return { projectId: "", pipeName: "", jobId: "" };
}

export const GetZZJobTestResponse = {
  encode(message: GetZZJobTestResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.pipeName !== "") {
      writer.uint32(18).string(message.pipeName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): GetZZJobTestResponse {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetZZJobTestResponse();
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

          message.pipeName = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.jobId = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): GetZZJobTestResponse {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      pipeName: isSet(object.pipeName) ? globalThis.String(object.pipeName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
    };
  },

  toJSON(message: GetZZJobTestResponse): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.pipeName !== "") {
      obj.pipeName = message.pipeName;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetZZJobTestResponse>, I>>(base?: I): GetZZJobTestResponse {
    return GetZZJobTestResponse.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetZZJobTestResponse>, I>>(object: I): GetZZJobTestResponse {
    const message = createBaseGetZZJobTestResponse();
    message.projectId = object.projectId ?? "";
    message.pipeName = object.pipeName ?? "";
    message.jobId = object.jobId ?? "";
    return message;
  },
};

function createBaseEnsureStreamRecRequest(): EnsureStreamRecRequest {
  return { projectId: "", streamId: "" };
}

export const EnsureStreamRecRequest = {
  encode(message: EnsureStreamRecRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.streamId !== "") {
      writer.uint32(18).string(message.streamId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): EnsureStreamRecRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseEnsureStreamRecRequest();
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

          message.streamId = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): EnsureStreamRecRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
    };
  },

  toJSON(message: EnsureStreamRecRequest): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<EnsureStreamRecRequest>, I>>(base?: I): EnsureStreamRecRequest {
    return EnsureStreamRecRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<EnsureStreamRecRequest>, I>>(object: I): EnsureStreamRecRequest {
    const message = createBaseEnsureStreamRecRequest();
    message.projectId = object.projectId ?? "";
    message.streamId = object.streamId ?? "";
    return message;
  },
};

function createBaseEnsureJobStreamConnectorRecRequest(): EnsureJobStreamConnectorRecRequest {
  return { projectId: "", streamId: "", jobId: "", key: "", connectorType: "" };
}

export const EnsureJobStreamConnectorRecRequest = {
  encode(message: EnsureJobStreamConnectorRecRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.streamId !== "") {
      writer.uint32(18).string(message.streamId);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.key !== "") {
      writer.uint32(34).string(message.key);
    }
    if (message.connectorType !== "") {
      writer.uint32(42).string(message.connectorType);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): EnsureJobStreamConnectorRecRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseEnsureJobStreamConnectorRecRequest();
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

          message.streamId = reader.string();
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

          message.key = reader.string();
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.connectorType = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): EnsureJobStreamConnectorRecRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      key: isSet(object.key) ? globalThis.String(object.key) : "",
      connectorType: isSet(object.connectorType) ? globalThis.String(object.connectorType) : "",
    };
  },

  toJSON(message: EnsureJobStreamConnectorRecRequest): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.key !== "") {
      obj.key = message.key;
    }
    if (message.connectorType !== "") {
      obj.connectorType = message.connectorType;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<EnsureJobStreamConnectorRecRequest>, I>>(
    base?: I,
  ): EnsureJobStreamConnectorRecRequest {
    return EnsureJobStreamConnectorRecRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<EnsureJobStreamConnectorRecRequest>, I>>(
    object: I,
  ): EnsureJobStreamConnectorRecRequest {
    const message = createBaseEnsureJobStreamConnectorRecRequest();
    message.projectId = object.projectId ?? "";
    message.streamId = object.streamId ?? "";
    message.jobId = object.jobId ?? "";
    message.key = object.key ?? "";
    message.connectorType = object.connectorType ?? "";
    return message;
  },
};

function createBaseGetJobStreamConnectorRecsRequest(): GetJobStreamConnectorRecsRequest {
  return { projectId: "", jobId: "", key: "", connectorType: "" };
}

export const GetJobStreamConnectorRecsRequest = {
  encode(message: GetJobStreamConnectorRecsRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.jobId !== "") {
      writer.uint32(18).string(message.jobId);
    }
    if (message.key !== "") {
      writer.uint32(26).string(message.key);
    }
    if (message.connectorType !== "") {
      writer.uint32(34).string(message.connectorType);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): GetJobStreamConnectorRecsRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetJobStreamConnectorRecsRequest();
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

          message.jobId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.key = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.connectorType = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): GetJobStreamConnectorRecsRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      key: isSet(object.key) ? globalThis.String(object.key) : "",
      connectorType: isSet(object.connectorType) ? globalThis.String(object.connectorType) : "",
    };
  },

  toJSON(message: GetJobStreamConnectorRecsRequest): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.key !== "") {
      obj.key = message.key;
    }
    if (message.connectorType !== "") {
      obj.connectorType = message.connectorType;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetJobStreamConnectorRecsRequest>, I>>(
    base?: I,
  ): GetJobStreamConnectorRecsRequest {
    return GetJobStreamConnectorRecsRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobStreamConnectorRecsRequest>, I>>(
    object: I,
  ): GetJobStreamConnectorRecsRequest {
    const message = createBaseGetJobStreamConnectorRecsRequest();
    message.projectId = object.projectId ?? "";
    message.jobId = object.jobId ?? "";
    message.key = object.key ?? "";
    message.connectorType = object.connectorType ?? "";
    return message;
  },
};

function createBaseJobStreamConnectorRecord(): JobStreamConnectorRecord {
  return { projectId: "", jobId: "", timeCreated: undefined, streamId: "", key: "", connectorType: "" };
}

export const JobStreamConnectorRecord = {
  encode(message: JobStreamConnectorRecord, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.jobId !== "") {
      writer.uint32(18).string(message.jobId);
    }
    if (message.timeCreated !== undefined) {
      Timestamp.encode(toTimestamp(message.timeCreated), writer.uint32(26).fork()).ldelim();
    }
    if (message.streamId !== "") {
      writer.uint32(34).string(message.streamId);
    }
    if (message.key !== "") {
      writer.uint32(42).string(message.key);
    }
    if (message.connectorType !== "") {
      writer.uint32(50).string(message.connectorType);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): JobStreamConnectorRecord {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseJobStreamConnectorRecord();
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

          message.jobId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.timeCreated = fromTimestamp(Timestamp.decode(reader, reader.uint32()));
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.streamId = reader.string();
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.key = reader.string();
          continue;
        case 6:
          if (tag !== 50) {
            break;
          }

          message.connectorType = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): JobStreamConnectorRecord {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      timeCreated: isSet(object.timeCreated) ? fromJsonTimestamp(object.timeCreated) : undefined,
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
      key: isSet(object.key) ? globalThis.String(object.key) : "",
      connectorType: isSet(object.connectorType) ? globalThis.String(object.connectorType) : "",
    };
  },

  toJSON(message: JobStreamConnectorRecord): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.timeCreated !== undefined) {
      obj.timeCreated = message.timeCreated.toISOString();
    }
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    if (message.key !== "") {
      obj.key = message.key;
    }
    if (message.connectorType !== "") {
      obj.connectorType = message.connectorType;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<JobStreamConnectorRecord>, I>>(base?: I): JobStreamConnectorRecord {
    return JobStreamConnectorRecord.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<JobStreamConnectorRecord>, I>>(object: I): JobStreamConnectorRecord {
    const message = createBaseJobStreamConnectorRecord();
    message.projectId = object.projectId ?? "";
    message.jobId = object.jobId ?? "";
    message.timeCreated = object.timeCreated ?? undefined;
    message.streamId = object.streamId ?? "";
    message.key = object.key ?? "";
    message.connectorType = object.connectorType ?? "";
    return message;
  },
};

function createBaseGetJobDatapointsRequest(): GetJobDatapointsRequest {
  return { projectId: "", pipeName: "", jobId: "", ioType: "", order: "", limit: 0, key: "" };
}

export const GetJobDatapointsRequest = {
  encode(message: GetJobDatapointsRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.pipeName !== "") {
      writer.uint32(18).string(message.pipeName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.ioType !== "") {
      writer.uint32(34).string(message.ioType);
    }
    if (message.order !== "") {
      writer.uint32(42).string(message.order);
    }
    if (message.limit !== 0) {
      writer.uint32(48).int32(message.limit);
    }
    if (message.key !== "") {
      writer.uint32(58).string(message.key);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): GetJobDatapointsRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetJobDatapointsRequest();
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

          message.pipeName = reader.string();
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

          message.ioType = reader.string();
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.order = reader.string();
          continue;
        case 6:
          if (tag !== 48) {
            break;
          }

          message.limit = reader.int32();
          continue;
        case 7:
          if (tag !== 58) {
            break;
          }

          message.key = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): GetJobDatapointsRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      pipeName: isSet(object.pipeName) ? globalThis.String(object.pipeName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      ioType: isSet(object.ioType) ? globalThis.String(object.ioType) : "",
      order: isSet(object.order) ? globalThis.String(object.order) : "",
      limit: isSet(object.limit) ? globalThis.Number(object.limit) : 0,
      key: isSet(object.key) ? globalThis.String(object.key) : "",
    };
  },

  toJSON(message: GetJobDatapointsRequest): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.pipeName !== "") {
      obj.pipeName = message.pipeName;
    }
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.ioType !== "") {
      obj.ioType = message.ioType;
    }
    if (message.order !== "") {
      obj.order = message.order;
    }
    if (message.limit !== 0) {
      obj.limit = Math.round(message.limit);
    }
    if (message.key !== "") {
      obj.key = message.key;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetJobDatapointsRequest>, I>>(base?: I): GetJobDatapointsRequest {
    return GetJobDatapointsRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobDatapointsRequest>, I>>(object: I): GetJobDatapointsRequest {
    const message = createBaseGetJobDatapointsRequest();
    message.projectId = object.projectId ?? "";
    message.pipeName = object.pipeName ?? "";
    message.jobId = object.jobId ?? "";
    message.ioType = object.ioType ?? "";
    message.order = object.order ?? "";
    message.limit = object.limit ?? 0;
    message.key = object.key ?? "";
    return message;
  },
};

function createBaseDatapointRecord(): DatapointRecord {
  return { datapointId: "", data: undefined };
}

export const DatapointRecord = {
  encode(message: DatapointRecord, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.datapointId !== "") {
      writer.uint32(10).string(message.datapointId);
    }
    if (message.data !== undefined) {
      Struct.encode(Struct.wrap(message.data), writer.uint32(18).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): DatapointRecord {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseDatapointRecord();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.datapointId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.data = Struct.unwrap(Struct.decode(reader, reader.uint32()));
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): DatapointRecord {
    return {
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
      data: isObject(object.data) ? object.data : undefined,
    };
  },

  toJSON(message: DatapointRecord): unknown {
    const obj: any = {};
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    if (message.data !== undefined) {
      obj.data = message.data;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<DatapointRecord>, I>>(base?: I): DatapointRecord {
    return DatapointRecord.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<DatapointRecord>, I>>(object: I): DatapointRecord {
    const message = createBaseDatapointRecord();
    message.datapointId = object.datapointId ?? "";
    message.data = object.data ?? undefined;
    return message;
  },
};

function createBaseJobInfo(): JobInfo {
  return { jobId: "", jobOutputKey: "" };
}

export const JobInfo = {
  encode(message: JobInfo, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.jobId !== "") {
      writer.uint32(10).string(message.jobId);
    }
    if (message.jobOutputKey !== "") {
      writer.uint32(18).string(message.jobOutputKey);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): JobInfo {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseJobInfo();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.jobId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.jobOutputKey = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): JobInfo {
    return {
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      jobOutputKey: isSet(object.jobOutputKey) ? globalThis.String(object.jobOutputKey) : "",
    };
  },

  toJSON(message: JobInfo): unknown {
    const obj: any = {};
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.jobOutputKey !== "") {
      obj.jobOutputKey = message.jobOutputKey;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<JobInfo>, I>>(base?: I): JobInfo {
    return JobInfo.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<JobInfo>, I>>(object: I): JobInfo {
    const message = createBaseJobInfo();
    message.jobId = object.jobId ?? "";
    message.jobOutputKey = object.jobOutputKey ?? "";
    return message;
  },
};

function createBaseAddDatapointRequest(): AddDatapointRequest {
  return { projectId: "", streamId: "", datapointId: "", jobInfo: undefined, data: undefined };
}

export const AddDatapointRequest = {
  encode(message: AddDatapointRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.streamId !== "") {
      writer.uint32(18).string(message.streamId);
    }
    if (message.datapointId !== "") {
      writer.uint32(26).string(message.datapointId);
    }
    if (message.jobInfo !== undefined) {
      JobInfo.encode(message.jobInfo, writer.uint32(34).fork()).ldelim();
    }
    if (message.data !== undefined) {
      Any.encode(message.data, writer.uint32(42).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): AddDatapointRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseAddDatapointRequest();
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

          message.streamId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.datapointId = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.jobInfo = JobInfo.decode(reader, reader.uint32());
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.data = Any.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): AddDatapointRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
      jobInfo: isSet(object.jobInfo) ? JobInfo.fromJSON(object.jobInfo) : undefined,
      data: isSet(object.data) ? Any.fromJSON(object.data) : undefined,
    };
  },

  toJSON(message: AddDatapointRequest): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    if (message.jobInfo !== undefined) {
      obj.jobInfo = JobInfo.toJSON(message.jobInfo);
    }
    if (message.data !== undefined) {
      obj.data = Any.toJSON(message.data);
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<AddDatapointRequest>, I>>(base?: I): AddDatapointRequest {
    return AddDatapointRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<AddDatapointRequest>, I>>(object: I): AddDatapointRequest {
    const message = createBaseAddDatapointRequest();
    message.projectId = object.projectId ?? "";
    message.streamId = object.streamId ?? "";
    message.datapointId = object.datapointId ?? "";
    message.jobInfo = (object.jobInfo !== undefined && object.jobInfo !== null)
      ? JobInfo.fromPartial(object.jobInfo)
      : undefined;
    message.data = (object.data !== undefined && object.data !== null) ? Any.fromPartial(object.data) : undefined;
    return message;
  },
};

function createBaseAddDatapointResponse(): AddDatapointResponse {
  return { datapointId: "" };
}

export const AddDatapointResponse = {
  encode(message: AddDatapointResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.datapointId !== "") {
      writer.uint32(10).string(message.datapointId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): AddDatapointResponse {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseAddDatapointResponse();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.datapointId = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): AddDatapointResponse {
    return { datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "" };
  },

  toJSON(message: AddDatapointResponse): unknown {
    const obj: any = {};
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<AddDatapointResponse>, I>>(base?: I): AddDatapointResponse {
    return AddDatapointResponse.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<AddDatapointResponse>, I>>(object: I): AddDatapointResponse {
    const message = createBaseAddDatapointResponse();
    message.datapointId = object.datapointId ?? "";
    return message;
  },
};

export type livestackService = typeof livestackService;
export const livestackService = {
  ensureStreamRec: {
    path: "/livestack.livestack/EnsureStreamRec",
    requestStream: false,
    responseStream: false,
    requestSerialize: (value: EnsureStreamRecRequest) => Buffer.from(EnsureStreamRecRequest.encode(value).finish()),
    requestDeserialize: (value: Buffer) => EnsureStreamRecRequest.decode(value),
    responseSerialize: (value: Empty) => Buffer.from(Empty.encode(value).finish()),
    responseDeserialize: (value: Buffer) => Empty.decode(value),
  },
  ensureJobStreamConnectorRec: {
    path: "/livestack.livestack/EnsureJobStreamConnectorRec",
    requestStream: false,
    responseStream: false,
    requestSerialize: (value: EnsureJobStreamConnectorRecRequest) =>
      Buffer.from(EnsureJobStreamConnectorRecRequest.encode(value).finish()),
    requestDeserialize: (value: Buffer) => EnsureJobStreamConnectorRecRequest.decode(value),
    responseSerialize: (value: Empty) => Buffer.from(Empty.encode(value).finish()),
    responseDeserialize: (value: Buffer) => Empty.decode(value),
  },
  getJobStreamConnectorRecs: {
    path: "/livestack.livestack/GetJobStreamConnectorRecs",
    requestStream: false,
    responseStream: true,
    requestSerialize: (value: GetJobStreamConnectorRecsRequest) =>
      Buffer.from(GetJobStreamConnectorRecsRequest.encode(value).finish()),
    requestDeserialize: (value: Buffer) => GetJobStreamConnectorRecsRequest.decode(value),
    responseSerialize: (value: JobStreamConnectorRecord) =>
      Buffer.from(JobStreamConnectorRecord.encode(value).finish()),
    responseDeserialize: (value: Buffer) => JobStreamConnectorRecord.decode(value),
  },
  getJobDatapoints: {
    path: "/livestack.livestack/GetJobDatapoints",
    requestStream: false,
    responseStream: false,
    requestSerialize: (value: GetJobDatapointsRequest) => Buffer.from(GetJobDatapointsRequest.encode(value).finish()),
    requestDeserialize: (value: Buffer) => GetJobDatapointsRequest.decode(value),
    responseSerialize: (value: DatapointRecord) => Buffer.from(DatapointRecord.encode(value).finish()),
    responseDeserialize: (value: Buffer) => DatapointRecord.decode(value),
  },
  addDatapoint: {
    path: "/livestack.livestack/AddDatapoint",
    requestStream: false,
    responseStream: false,
    requestSerialize: (value: AddDatapointRequest) => Buffer.from(AddDatapointRequest.encode(value).finish()),
    requestDeserialize: (value: Buffer) => AddDatapointRequest.decode(value),
    responseSerialize: (value: AddDatapointResponse) => Buffer.from(AddDatapointResponse.encode(value).finish()),
    responseDeserialize: (value: Buffer) => AddDatapointResponse.decode(value),
  },
} as const;

export interface livestackServer extends UntypedServiceImplementation {
  ensureStreamRec: handleUnaryCall<EnsureStreamRecRequest, Empty>;
  ensureJobStreamConnectorRec: handleUnaryCall<EnsureJobStreamConnectorRecRequest, Empty>;
  getJobStreamConnectorRecs: handleServerStreamingCall<GetJobStreamConnectorRecsRequest, JobStreamConnectorRecord>;
  getJobDatapoints: handleUnaryCall<GetJobDatapointsRequest, DatapointRecord>;
  addDatapoint: handleUnaryCall<AddDatapointRequest, AddDatapointResponse>;
}

export interface livestackClient extends Client {
  ensureStreamRec(
    request: EnsureStreamRecRequest,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  ensureStreamRec(
    request: EnsureStreamRecRequest,
    metadata: Metadata,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  ensureStreamRec(
    request: EnsureStreamRecRequest,
    metadata: Metadata,
    options: Partial<CallOptions>,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  ensureJobStreamConnectorRec(
    request: EnsureJobStreamConnectorRecRequest,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  ensureJobStreamConnectorRec(
    request: EnsureJobStreamConnectorRecRequest,
    metadata: Metadata,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  ensureJobStreamConnectorRec(
    request: EnsureJobStreamConnectorRecRequest,
    metadata: Metadata,
    options: Partial<CallOptions>,
    callback: (error: ServiceError | null, response: Empty) => void,
  ): ClientUnaryCall;
  getJobStreamConnectorRecs(
    request: GetJobStreamConnectorRecsRequest,
    options?: Partial<CallOptions>,
  ): ClientReadableStream<JobStreamConnectorRecord>;
  getJobStreamConnectorRecs(
    request: GetJobStreamConnectorRecsRequest,
    metadata?: Metadata,
    options?: Partial<CallOptions>,
  ): ClientReadableStream<JobStreamConnectorRecord>;
  getJobDatapoints(
    request: GetJobDatapointsRequest,
    callback: (error: ServiceError | null, response: DatapointRecord) => void,
  ): ClientUnaryCall;
  getJobDatapoints(
    request: GetJobDatapointsRequest,
    metadata: Metadata,
    callback: (error: ServiceError | null, response: DatapointRecord) => void,
  ): ClientUnaryCall;
  getJobDatapoints(
    request: GetJobDatapointsRequest,
    metadata: Metadata,
    options: Partial<CallOptions>,
    callback: (error: ServiceError | null, response: DatapointRecord) => void,
  ): ClientUnaryCall;
  addDatapoint(
    request: AddDatapointRequest,
    callback: (error: ServiceError | null, response: AddDatapointResponse) => void,
  ): ClientUnaryCall;
  addDatapoint(
    request: AddDatapointRequest,
    metadata: Metadata,
    callback: (error: ServiceError | null, response: AddDatapointResponse) => void,
  ): ClientUnaryCall;
  addDatapoint(
    request: AddDatapointRequest,
    metadata: Metadata,
    options: Partial<CallOptions>,
    callback: (error: ServiceError | null, response: AddDatapointResponse) => void,
  ): ClientUnaryCall;
}

export const livestackClient = makeGenericClientConstructor(livestackService, "livestack.livestack") as unknown as {
  new (address: string, credentials: ChannelCredentials, options?: Partial<ClientOptions>): livestackClient;
  service: typeof livestackService;
  serviceName: string;
};

type Builtin = Date | Function | Uint8Array | string | number | boolean | undefined;

export type DeepPartial<T> = T extends Builtin ? T
  : T extends globalThis.Array<infer U> ? globalThis.Array<DeepPartial<U>>
  : T extends ReadonlyArray<infer U> ? ReadonlyArray<DeepPartial<U>>
  : T extends {} ? { [K in keyof T]?: DeepPartial<T[K]> }
  : Partial<T>;

type KeysOfUnion<T> = T extends T ? keyof T : never;
export type Exact<P, I extends P> = P extends Builtin ? P
  : P & { [K in keyof P]: Exact<P[K], I[K]> } & { [K in Exclude<keyof I, KeysOfUnion<P>>]: never };

function toTimestamp(date: Date): Timestamp {
  const seconds = Math.trunc(date.getTime() / 1_000);
  const nanos = (date.getTime() % 1_000) * 1_000_000;
  return { seconds, nanos };
}

function fromTimestamp(t: Timestamp): Date {
  let millis = (t.seconds || 0) * 1_000;
  millis += (t.nanos || 0) / 1_000_000;
  return new globalThis.Date(millis);
}

function fromJsonTimestamp(o: any): Date {
  if (o instanceof globalThis.Date) {
    return o;
  } else if (typeof o === "string") {
    return new globalThis.Date(o);
  } else {
    return fromTimestamp(Timestamp.fromJSON(o));
  }
}

function isObject(value: any): boolean {
  return typeof value === "object" && value !== null;
}

function isSet(value: any): boolean {
  return value !== null && value !== undefined;
}
