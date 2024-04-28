/* eslint-disable */
import Long from "long";
import type { CallContext, CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";
import { Empty } from "./google/protobuf/empty";

export const protobufPackage = "livestack";

export enum SubType {
  fromStart = 0,
  fromNow = 1,
  UNRECOGNIZED = -1,
}

export function subTypeFromJSON(object: any): SubType {
  switch (object) {
    case 0:
    case "fromStart":
      return SubType.fromStart;
    case 1:
    case "fromNow":
      return SubType.fromNow;
    case -1:
    case "UNRECOGNIZED":
    default:
      return SubType.UNRECOGNIZED;
  }
}

export function subTypeToJSON(object: SubType): string {
  switch (object) {
    case SubType.fromStart:
      return "fromStart";
    case SubType.fromNow:
      return "fromNow";
    case SubType.UNRECOGNIZED:
    default:
      return "UNRECOGNIZED";
  }
}

export interface EnsureStreamRequest {
  project_uuid: string;
  stream_id: string;
  json_schema_str?: string | undefined;
}

export interface JobInfo {
  jobId: string;
  outputTag: string;
}

export interface ParentDataPointInfo {
  streamId: string;
  datapointId: string;
}

export interface StreamPubMessage {
  projectUuid: string;
  streamId: string;
  dataStr: string;
  parentDatapoints: ParentDataPointInfo[];
  jobInfo?: JobInfo | undefined;
}

export interface StreamPubSuccessResult {
  chunkId: string;
  datapointId: string;
}

export interface StreamPubValidationFailure {
  errorMessage: string;
  datapointId: string;
}

export interface StreamPubResult {
  success?: StreamPubSuccessResult | undefined;
  validationFailure?: StreamPubValidationFailure | undefined;
}

export interface SubRequest {
  projectUuid: string;
  uniqueName: string;
  subType: SubType;
}

export interface StreamDatapoint {
  timestamp: number;
  chunkId: string;
  dataStr: string;
  datapointId: string;
}

export interface ValuesByReverseIndexRequest {
  projectUuid: string;
  uniqueName: string;
  lastN: number;
}

export interface DataPointOrNull {
  datapoint?: StreamDatapoint | undefined;
  null_response?: Empty | undefined;
}

export interface ValuesByReverseIndexResponse {
  datapoints: DataPointOrNull[];
}

export interface LastValueRequest {
  projectUuid: string;
  uniqueName: string;
}

export interface LastValueResponse {
  datapoint?: StreamDatapoint | undefined;
  null_response?: Empty | undefined;
}

function createBaseEnsureStreamRequest(): EnsureStreamRequest {
  return { project_uuid: "", stream_id: "", json_schema_str: undefined };
}

export const EnsureStreamRequest = {
  encode(message: EnsureStreamRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.project_uuid !== "") {
      writer.uint32(10).string(message.project_uuid);
    }
    if (message.stream_id !== "") {
      writer.uint32(18).string(message.stream_id);
    }
    if (message.json_schema_str !== undefined) {
      writer.uint32(26).string(message.json_schema_str);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): EnsureStreamRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseEnsureStreamRequest();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.project_uuid = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.stream_id = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.json_schema_str = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): EnsureStreamRequest {
    return {
      project_uuid: isSet(object.project_uuid) ? globalThis.String(object.project_uuid) : "",
      stream_id: isSet(object.stream_id) ? globalThis.String(object.stream_id) : "",
      json_schema_str: isSet(object.json_schema_str) ? globalThis.String(object.json_schema_str) : undefined,
    };
  },

  toJSON(message: EnsureStreamRequest): unknown {
    const obj: any = {};
    if (message.project_uuid !== "") {
      obj.project_uuid = message.project_uuid;
    }
    if (message.stream_id !== "") {
      obj.stream_id = message.stream_id;
    }
    if (message.json_schema_str !== undefined) {
      obj.json_schema_str = message.json_schema_str;
    }
    return obj;
  },

  create(base?: DeepPartial<EnsureStreamRequest>): EnsureStreamRequest {
    return EnsureStreamRequest.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<EnsureStreamRequest>): EnsureStreamRequest {
    const message = createBaseEnsureStreamRequest();
    message.project_uuid = object.project_uuid ?? "";
    message.stream_id = object.stream_id ?? "";
    message.json_schema_str = object.json_schema_str ?? undefined;
    return message;
  },
};

function createBaseJobInfo(): JobInfo {
  return { jobId: "", outputTag: "" };
}

export const JobInfo = {
  encode(message: JobInfo, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.jobId !== "") {
      writer.uint32(10).string(message.jobId);
    }
    if (message.outputTag !== "") {
      writer.uint32(18).string(message.outputTag);
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

          message.outputTag = reader.string();
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
      outputTag: isSet(object.outputTag) ? globalThis.String(object.outputTag) : "",
    };
  },

  toJSON(message: JobInfo): unknown {
    const obj: any = {};
    if (message.jobId !== "") {
      obj.jobId = message.jobId;
    }
    if (message.outputTag !== "") {
      obj.outputTag = message.outputTag;
    }
    return obj;
  },

  create(base?: DeepPartial<JobInfo>): JobInfo {
    return JobInfo.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<JobInfo>): JobInfo {
    const message = createBaseJobInfo();
    message.jobId = object.jobId ?? "";
    message.outputTag = object.outputTag ?? "";
    return message;
  },
};

function createBaseParentDataPointInfo(): ParentDataPointInfo {
  return { streamId: "", datapointId: "" };
}

export const ParentDataPointInfo = {
  encode(message: ParentDataPointInfo, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.streamId !== "") {
      writer.uint32(10).string(message.streamId);
    }
    if (message.datapointId !== "") {
      writer.uint32(18).string(message.datapointId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ParentDataPointInfo {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseParentDataPointInfo();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.streamId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
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

  fromJSON(object: any): ParentDataPointInfo {
    return {
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
    };
  },

  toJSON(message: ParentDataPointInfo): unknown {
    const obj: any = {};
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    return obj;
  },

  create(base?: DeepPartial<ParentDataPointInfo>): ParentDataPointInfo {
    return ParentDataPointInfo.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ParentDataPointInfo>): ParentDataPointInfo {
    const message = createBaseParentDataPointInfo();
    message.streamId = object.streamId ?? "";
    message.datapointId = object.datapointId ?? "";
    return message;
  },
};

function createBaseStreamPubMessage(): StreamPubMessage {
  return { projectUuid: "", streamId: "", dataStr: "", parentDatapoints: [], jobInfo: undefined };
}

export const StreamPubMessage = {
  encode(message: StreamPubMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.streamId !== "") {
      writer.uint32(18).string(message.streamId);
    }
    if (message.dataStr !== "") {
      writer.uint32(26).string(message.dataStr);
    }
    for (const v of message.parentDatapoints) {
      ParentDataPointInfo.encode(v!, writer.uint32(34).fork()).ldelim();
    }
    if (message.jobInfo !== undefined) {
      JobInfo.encode(message.jobInfo, writer.uint32(42).fork()).ldelim();
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

          message.projectUuid = reader.string();
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

          message.dataStr = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.parentDatapoints.push(ParentDataPointInfo.decode(reader, reader.uint32()));
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.jobInfo = JobInfo.decode(reader, reader.uint32());
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
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      streamId: isSet(object.streamId) ? globalThis.String(object.streamId) : "",
      dataStr: isSet(object.dataStr) ? globalThis.String(object.dataStr) : "",
      parentDatapoints: globalThis.Array.isArray(object?.parentDatapoints)
        ? object.parentDatapoints.map((e: any) => ParentDataPointInfo.fromJSON(e))
        : [],
      jobInfo: isSet(object.jobInfo) ? JobInfo.fromJSON(object.jobInfo) : undefined,
    };
  },

  toJSON(message: StreamPubMessage): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.streamId !== "") {
      obj.streamId = message.streamId;
    }
    if (message.dataStr !== "") {
      obj.dataStr = message.dataStr;
    }
    if (message.parentDatapoints?.length) {
      obj.parentDatapoints = message.parentDatapoints.map((e) => ParentDataPointInfo.toJSON(e));
    }
    if (message.jobInfo !== undefined) {
      obj.jobInfo = JobInfo.toJSON(message.jobInfo);
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubMessage>): StreamPubMessage {
    return StreamPubMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubMessage>): StreamPubMessage {
    const message = createBaseStreamPubMessage();
    message.projectUuid = object.projectUuid ?? "";
    message.streamId = object.streamId ?? "";
    message.dataStr = object.dataStr ?? "";
    message.parentDatapoints = object.parentDatapoints?.map((e) => ParentDataPointInfo.fromPartial(e)) || [];
    message.jobInfo = (object.jobInfo !== undefined && object.jobInfo !== null)
      ? JobInfo.fromPartial(object.jobInfo)
      : undefined;
    return message;
  },
};

function createBaseStreamPubSuccessResult(): StreamPubSuccessResult {
  return { chunkId: "", datapointId: "" };
}

export const StreamPubSuccessResult = {
  encode(message: StreamPubSuccessResult, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.chunkId !== "") {
      writer.uint32(10).string(message.chunkId);
    }
    if (message.datapointId !== "") {
      writer.uint32(18).string(message.datapointId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): StreamPubSuccessResult {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseStreamPubSuccessResult();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.chunkId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
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

  fromJSON(object: any): StreamPubSuccessResult {
    return {
      chunkId: isSet(object.chunkId) ? globalThis.String(object.chunkId) : "",
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
    };
  },

  toJSON(message: StreamPubSuccessResult): unknown {
    const obj: any = {};
    if (message.chunkId !== "") {
      obj.chunkId = message.chunkId;
    }
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubSuccessResult>): StreamPubSuccessResult {
    return StreamPubSuccessResult.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubSuccessResult>): StreamPubSuccessResult {
    const message = createBaseStreamPubSuccessResult();
    message.chunkId = object.chunkId ?? "";
    message.datapointId = object.datapointId ?? "";
    return message;
  },
};

function createBaseStreamPubValidationFailure(): StreamPubValidationFailure {
  return { errorMessage: "", datapointId: "" };
}

export const StreamPubValidationFailure = {
  encode(message: StreamPubValidationFailure, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.errorMessage !== "") {
      writer.uint32(10).string(message.errorMessage);
    }
    if (message.datapointId !== "") {
      writer.uint32(18).string(message.datapointId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): StreamPubValidationFailure {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseStreamPubValidationFailure();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.errorMessage = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
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

  fromJSON(object: any): StreamPubValidationFailure {
    return {
      errorMessage: isSet(object.errorMessage) ? globalThis.String(object.errorMessage) : "",
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
    };
  },

  toJSON(message: StreamPubValidationFailure): unknown {
    const obj: any = {};
    if (message.errorMessage !== "") {
      obj.errorMessage = message.errorMessage;
    }
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubValidationFailure>): StreamPubValidationFailure {
    return StreamPubValidationFailure.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubValidationFailure>): StreamPubValidationFailure {
    const message = createBaseStreamPubValidationFailure();
    message.errorMessage = object.errorMessage ?? "";
    message.datapointId = object.datapointId ?? "";
    return message;
  },
};

function createBaseStreamPubResult(): StreamPubResult {
  return { success: undefined, validationFailure: undefined };
}

export const StreamPubResult = {
  encode(message: StreamPubResult, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.success !== undefined) {
      StreamPubSuccessResult.encode(message.success, writer.uint32(10).fork()).ldelim();
    }
    if (message.validationFailure !== undefined) {
      StreamPubValidationFailure.encode(message.validationFailure, writer.uint32(18).fork()).ldelim();
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

          message.success = StreamPubSuccessResult.decode(reader, reader.uint32());
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.validationFailure = StreamPubValidationFailure.decode(reader, reader.uint32());
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
    return {
      success: isSet(object.success) ? StreamPubSuccessResult.fromJSON(object.success) : undefined,
      validationFailure: isSet(object.validationFailure)
        ? StreamPubValidationFailure.fromJSON(object.validationFailure)
        : undefined,
    };
  },

  toJSON(message: StreamPubResult): unknown {
    const obj: any = {};
    if (message.success !== undefined) {
      obj.success = StreamPubSuccessResult.toJSON(message.success);
    }
    if (message.validationFailure !== undefined) {
      obj.validationFailure = StreamPubValidationFailure.toJSON(message.validationFailure);
    }
    return obj;
  },

  create(base?: DeepPartial<StreamPubResult>): StreamPubResult {
    return StreamPubResult.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamPubResult>): StreamPubResult {
    const message = createBaseStreamPubResult();
    message.success = (object.success !== undefined && object.success !== null)
      ? StreamPubSuccessResult.fromPartial(object.success)
      : undefined;
    message.validationFailure = (object.validationFailure !== undefined && object.validationFailure !== null)
      ? StreamPubValidationFailure.fromPartial(object.validationFailure)
      : undefined;
    return message;
  },
};

function createBaseSubRequest(): SubRequest {
  return { projectUuid: "", uniqueName: "", subType: 0 };
}

export const SubRequest = {
  encode(message: SubRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.uniqueName !== "") {
      writer.uint32(18).string(message.uniqueName);
    }
    if (message.subType !== 0) {
      writer.uint32(40).int32(message.subType);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): SubRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseSubRequest();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.projectUuid = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.uniqueName = reader.string();
          continue;
        case 5:
          if (tag !== 40) {
            break;
          }

          message.subType = reader.int32() as any;
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): SubRequest {
    return {
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      uniqueName: isSet(object.uniqueName) ? globalThis.String(object.uniqueName) : "",
      subType: isSet(object.subType) ? subTypeFromJSON(object.subType) : 0,
    };
  },

  toJSON(message: SubRequest): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.uniqueName !== "") {
      obj.uniqueName = message.uniqueName;
    }
    if (message.subType !== 0) {
      obj.subType = subTypeToJSON(message.subType);
    }
    return obj;
  },

  create(base?: DeepPartial<SubRequest>): SubRequest {
    return SubRequest.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<SubRequest>): SubRequest {
    const message = createBaseSubRequest();
    message.projectUuid = object.projectUuid ?? "";
    message.uniqueName = object.uniqueName ?? "";
    message.subType = object.subType ?? 0;
    return message;
  },
};

function createBaseStreamDatapoint(): StreamDatapoint {
  return { timestamp: 0, chunkId: "", dataStr: "", datapointId: "" };
}

export const StreamDatapoint = {
  encode(message: StreamDatapoint, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.timestamp !== 0) {
      writer.uint32(8).uint64(message.timestamp);
    }
    if (message.chunkId !== "") {
      writer.uint32(18).string(message.chunkId);
    }
    if (message.dataStr !== "") {
      writer.uint32(26).string(message.dataStr);
    }
    if (message.datapointId !== "") {
      writer.uint32(34).string(message.datapointId);
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

          message.timestamp = longToNumber(reader.uint64() as Long);
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.chunkId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.dataStr = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
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

  fromJSON(object: any): StreamDatapoint {
    return {
      timestamp: isSet(object.timestamp) ? globalThis.Number(object.timestamp) : 0,
      chunkId: isSet(object.chunkId) ? globalThis.String(object.chunkId) : "",
      dataStr: isSet(object.dataStr) ? globalThis.String(object.dataStr) : "",
      datapointId: isSet(object.datapointId) ? globalThis.String(object.datapointId) : "",
    };
  },

  toJSON(message: StreamDatapoint): unknown {
    const obj: any = {};
    if (message.timestamp !== 0) {
      obj.timestamp = Math.round(message.timestamp);
    }
    if (message.chunkId !== "") {
      obj.chunkId = message.chunkId;
    }
    if (message.dataStr !== "") {
      obj.dataStr = message.dataStr;
    }
    if (message.datapointId !== "") {
      obj.datapointId = message.datapointId;
    }
    return obj;
  },

  create(base?: DeepPartial<StreamDatapoint>): StreamDatapoint {
    return StreamDatapoint.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<StreamDatapoint>): StreamDatapoint {
    const message = createBaseStreamDatapoint();
    message.timestamp = object.timestamp ?? 0;
    message.chunkId = object.chunkId ?? "";
    message.dataStr = object.dataStr ?? "";
    message.datapointId = object.datapointId ?? "";
    return message;
  },
};

function createBaseValuesByReverseIndexRequest(): ValuesByReverseIndexRequest {
  return { projectUuid: "", uniqueName: "", lastN: 0 };
}

export const ValuesByReverseIndexRequest = {
  encode(message: ValuesByReverseIndexRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.uniqueName !== "") {
      writer.uint32(18).string(message.uniqueName);
    }
    if (message.lastN !== 0) {
      writer.uint32(24).uint64(message.lastN);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ValuesByReverseIndexRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseValuesByReverseIndexRequest();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.projectUuid = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.uniqueName = reader.string();
          continue;
        case 3:
          if (tag !== 24) {
            break;
          }

          message.lastN = longToNumber(reader.uint64() as Long);
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ValuesByReverseIndexRequest {
    return {
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      uniqueName: isSet(object.uniqueName) ? globalThis.String(object.uniqueName) : "",
      lastN: isSet(object.lastN) ? globalThis.Number(object.lastN) : 0,
    };
  },

  toJSON(message: ValuesByReverseIndexRequest): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.uniqueName !== "") {
      obj.uniqueName = message.uniqueName;
    }
    if (message.lastN !== 0) {
      obj.lastN = Math.round(message.lastN);
    }
    return obj;
  },

  create(base?: DeepPartial<ValuesByReverseIndexRequest>): ValuesByReverseIndexRequest {
    return ValuesByReverseIndexRequest.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ValuesByReverseIndexRequest>): ValuesByReverseIndexRequest {
    const message = createBaseValuesByReverseIndexRequest();
    message.projectUuid = object.projectUuid ?? "";
    message.uniqueName = object.uniqueName ?? "";
    message.lastN = object.lastN ?? 0;
    return message;
  },
};

function createBaseDataPointOrNull(): DataPointOrNull {
  return { datapoint: undefined, null_response: undefined };
}

export const DataPointOrNull = {
  encode(message: DataPointOrNull, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.datapoint !== undefined) {
      StreamDatapoint.encode(message.datapoint, writer.uint32(10).fork()).ldelim();
    }
    if (message.null_response !== undefined) {
      Empty.encode(message.null_response, writer.uint32(18).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): DataPointOrNull {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseDataPointOrNull();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.datapoint = StreamDatapoint.decode(reader, reader.uint32());
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.null_response = Empty.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): DataPointOrNull {
    return {
      datapoint: isSet(object.datapoint) ? StreamDatapoint.fromJSON(object.datapoint) : undefined,
      null_response: isSet(object.null_response) ? Empty.fromJSON(object.null_response) : undefined,
    };
  },

  toJSON(message: DataPointOrNull): unknown {
    const obj: any = {};
    if (message.datapoint !== undefined) {
      obj.datapoint = StreamDatapoint.toJSON(message.datapoint);
    }
    if (message.null_response !== undefined) {
      obj.null_response = Empty.toJSON(message.null_response);
    }
    return obj;
  },

  create(base?: DeepPartial<DataPointOrNull>): DataPointOrNull {
    return DataPointOrNull.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<DataPointOrNull>): DataPointOrNull {
    const message = createBaseDataPointOrNull();
    message.datapoint = (object.datapoint !== undefined && object.datapoint !== null)
      ? StreamDatapoint.fromPartial(object.datapoint)
      : undefined;
    message.null_response = (object.null_response !== undefined && object.null_response !== null)
      ? Empty.fromPartial(object.null_response)
      : undefined;
    return message;
  },
};

function createBaseValuesByReverseIndexResponse(): ValuesByReverseIndexResponse {
  return { datapoints: [] };
}

export const ValuesByReverseIndexResponse = {
  encode(message: ValuesByReverseIndexResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    for (const v of message.datapoints) {
      DataPointOrNull.encode(v!, writer.uint32(10).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ValuesByReverseIndexResponse {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseValuesByReverseIndexResponse();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.datapoints.push(DataPointOrNull.decode(reader, reader.uint32()));
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ValuesByReverseIndexResponse {
    return {
      datapoints: globalThis.Array.isArray(object?.datapoints)
        ? object.datapoints.map((e: any) => DataPointOrNull.fromJSON(e))
        : [],
    };
  },

  toJSON(message: ValuesByReverseIndexResponse): unknown {
    const obj: any = {};
    if (message.datapoints?.length) {
      obj.datapoints = message.datapoints.map((e) => DataPointOrNull.toJSON(e));
    }
    return obj;
  },

  create(base?: DeepPartial<ValuesByReverseIndexResponse>): ValuesByReverseIndexResponse {
    return ValuesByReverseIndexResponse.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ValuesByReverseIndexResponse>): ValuesByReverseIndexResponse {
    const message = createBaseValuesByReverseIndexResponse();
    message.datapoints = object.datapoints?.map((e) => DataPointOrNull.fromPartial(e)) || [];
    return message;
  },
};

function createBaseLastValueRequest(): LastValueRequest {
  return { projectUuid: "", uniqueName: "" };
}

export const LastValueRequest = {
  encode(message: LastValueRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.uniqueName !== "") {
      writer.uint32(18).string(message.uniqueName);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): LastValueRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseLastValueRequest();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.projectUuid = reader.string();
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

  fromJSON(object: any): LastValueRequest {
    return {
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      uniqueName: isSet(object.uniqueName) ? globalThis.String(object.uniqueName) : "",
    };
  },

  toJSON(message: LastValueRequest): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.uniqueName !== "") {
      obj.uniqueName = message.uniqueName;
    }
    return obj;
  },

  create(base?: DeepPartial<LastValueRequest>): LastValueRequest {
    return LastValueRequest.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<LastValueRequest>): LastValueRequest {
    const message = createBaseLastValueRequest();
    message.projectUuid = object.projectUuid ?? "";
    message.uniqueName = object.uniqueName ?? "";
    return message;
  },
};

function createBaseLastValueResponse(): LastValueResponse {
  return { datapoint: undefined, null_response: undefined };
}

export const LastValueResponse = {
  encode(message: LastValueResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.datapoint !== undefined) {
      StreamDatapoint.encode(message.datapoint, writer.uint32(10).fork()).ldelim();
    }
    if (message.null_response !== undefined) {
      Empty.encode(message.null_response, writer.uint32(18).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): LastValueResponse {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseLastValueResponse();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.datapoint = StreamDatapoint.decode(reader, reader.uint32());
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.null_response = Empty.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): LastValueResponse {
    return {
      datapoint: isSet(object.datapoint) ? StreamDatapoint.fromJSON(object.datapoint) : undefined,
      null_response: isSet(object.null_response) ? Empty.fromJSON(object.null_response) : undefined,
    };
  },

  toJSON(message: LastValueResponse): unknown {
    const obj: any = {};
    if (message.datapoint !== undefined) {
      obj.datapoint = StreamDatapoint.toJSON(message.datapoint);
    }
    if (message.null_response !== undefined) {
      obj.null_response = Empty.toJSON(message.null_response);
    }
    return obj;
  },

  create(base?: DeepPartial<LastValueResponse>): LastValueResponse {
    return LastValueResponse.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<LastValueResponse>): LastValueResponse {
    const message = createBaseLastValueResponse();
    message.datapoint = (object.datapoint !== undefined && object.datapoint !== null)
      ? StreamDatapoint.fromPartial(object.datapoint)
      : undefined;
    message.null_response = (object.null_response !== undefined && object.null_response !== null)
      ? Empty.fromPartial(object.null_response)
      : undefined;
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
      requestType: SubRequest,
      requestStream: false,
      responseType: StreamDatapoint,
      responseStream: true,
      options: { idempotencyLevel: "NO_SIDE_EFFECTS" },
    },
    valuesByReverseIndex: {
      name: "valuesByReverseIndex",
      requestType: ValuesByReverseIndexRequest,
      requestStream: false,
      responseType: ValuesByReverseIndexResponse,
      responseStream: false,
      options: { idempotencyLevel: "NO_SIDE_EFFECTS" },
    },
    lastValue: {
      name: "lastValue",
      requestType: LastValueRequest,
      requestStream: false,
      responseType: LastValueResponse,
      responseStream: false,
      options: { idempotencyLevel: "NO_SIDE_EFFECTS" },
    },
    ensureStream: {
      name: "EnsureStream",
      requestType: EnsureStreamRequest,
      requestStream: false,
      responseType: Empty,
      responseStream: false,
      options: { idempotencyLevel: "IDEMPOTENT" },
    },
  },
} as const;

export interface StreamServiceImplementation<CallContextExt = {}> {
  pub(request: StreamPubMessage, context: CallContext & CallContextExt): Promise<DeepPartial<StreamPubResult>>;
  sub(
    request: SubRequest,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<StreamDatapoint>>;
  valuesByReverseIndex(
    request: ValuesByReverseIndexRequest,
    context: CallContext & CallContextExt,
  ): Promise<DeepPartial<ValuesByReverseIndexResponse>>;
  lastValue(request: LastValueRequest, context: CallContext & CallContextExt): Promise<DeepPartial<LastValueResponse>>;
  ensureStream(request: EnsureStreamRequest, context: CallContext & CallContextExt): Promise<DeepPartial<Empty>>;
}

export interface StreamServiceClient<CallOptionsExt = {}> {
  pub(request: DeepPartial<StreamPubMessage>, options?: CallOptions & CallOptionsExt): Promise<StreamPubResult>;
  sub(request: DeepPartial<SubRequest>, options?: CallOptions & CallOptionsExt): AsyncIterable<StreamDatapoint>;
  valuesByReverseIndex(
    request: DeepPartial<ValuesByReverseIndexRequest>,
    options?: CallOptions & CallOptionsExt,
  ): Promise<ValuesByReverseIndexResponse>;
  lastValue(request: DeepPartial<LastValueRequest>, options?: CallOptions & CallOptionsExt): Promise<LastValueResponse>;
  ensureStream(request: DeepPartial<EnsureStreamRequest>, options?: CallOptions & CallOptionsExt): Promise<Empty>;
}

type Builtin = Date | Function | Uint8Array | string | number | boolean | undefined;

export type DeepPartial<T> = T extends Builtin ? T
  : T extends globalThis.Array<infer U> ? globalThis.Array<DeepPartial<U>>
  : T extends ReadonlyArray<infer U> ? ReadonlyArray<DeepPartial<U>>
  : T extends {} ? { [K in keyof T]?: DeepPartial<T[K]> }
  : Partial<T>;

function longToNumber(long: Long): number {
  if (long.gt(globalThis.Number.MAX_SAFE_INTEGER)) {
    throw new globalThis.Error("Value is larger than Number.MAX_SAFE_INTEGER");
  }
  return long.toNumber();
}

if (_m0.util.Long !== Long) {
  _m0.util.Long = Long as any;
  _m0.configure();
}

function isSet(value: any): boolean {
  return value !== null && value !== undefined;
}

export type ServerStreamingMethodResult<Response> = { [Symbol.asyncIterator](): AsyncIterator<Response, void> };
