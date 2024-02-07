/* eslint-disable */
import _m0 from "protobufjs/minimal";
import { Empty } from "./google/protobuf/empty";
import { Struct } from "./google/protobuf/struct";
import { Timestamp } from "./google/protobuf/timestamp";

export const protobufPackage = "livestack";

export interface GetJobRecRequest {
  projectId: string;
  pipeName: string;
  jobId: string;
}

export interface GetJobRecResponse {
  /** The actual response data */
  jobData?:
    | JobRec
    | undefined;
  /** Signal that the response is null */
  nullResponse?: Empty | undefined;
}

export interface JobRec {
  projectId: string;
  pipeName: string;
  jobId: string;
  timeCreated: Date | undefined;
  jobParams: { [key: string]: any } | undefined;
  status: string;
}

function createBaseGetJobRecRequest(): GetJobRecRequest {
  return { projectId: "", pipeName: "", jobId: "" };
}

export const GetJobRecRequest = {
  encode(message: GetJobRecRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
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

  decode(input: _m0.Reader | Uint8Array, length?: number): GetJobRecRequest {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetJobRecRequest();
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

  fromJSON(object: any): GetJobRecRequest {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      pipeName: isSet(object.pipeName) ? globalThis.String(object.pipeName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
    };
  },

  toJSON(message: GetJobRecRequest): unknown {
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

  create<I extends Exact<DeepPartial<GetJobRecRequest>, I>>(base?: I): GetJobRecRequest {
    return GetJobRecRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobRecRequest>, I>>(object: I): GetJobRecRequest {
    const message = createBaseGetJobRecRequest();
    message.projectId = object.projectId ?? "";
    message.pipeName = object.pipeName ?? "";
    message.jobId = object.jobId ?? "";
    return message;
  },
};

function createBaseGetJobRecResponse(): GetJobRecResponse {
  return { jobData: undefined, nullResponse: undefined };
}

export const GetJobRecResponse = {
  encode(message: GetJobRecResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.jobData !== undefined) {
      JobRec.encode(message.jobData, writer.uint32(10).fork()).ldelim();
    }
    if (message.nullResponse !== undefined) {
      Empty.encode(message.nullResponse, writer.uint32(18).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): GetJobRecResponse {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseGetJobRecResponse();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.jobData = JobRec.decode(reader, reader.uint32());
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.nullResponse = Empty.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): GetJobRecResponse {
    return {
      jobData: isSet(object.jobData) ? JobRec.fromJSON(object.jobData) : undefined,
      nullResponse: isSet(object.nullResponse) ? Empty.fromJSON(object.nullResponse) : undefined,
    };
  },

  toJSON(message: GetJobRecResponse): unknown {
    const obj: any = {};
    if (message.jobData !== undefined) {
      obj.jobData = JobRec.toJSON(message.jobData);
    }
    if (message.nullResponse !== undefined) {
      obj.nullResponse = Empty.toJSON(message.nullResponse);
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetJobRecResponse>, I>>(base?: I): GetJobRecResponse {
    return GetJobRecResponse.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobRecResponse>, I>>(object: I): GetJobRecResponse {
    const message = createBaseGetJobRecResponse();
    message.jobData = (object.jobData !== undefined && object.jobData !== null)
      ? JobRec.fromPartial(object.jobData)
      : undefined;
    message.nullResponse = (object.nullResponse !== undefined && object.nullResponse !== null)
      ? Empty.fromPartial(object.nullResponse)
      : undefined;
    return message;
  },
};

function createBaseJobRec(): JobRec {
  return { projectId: "", pipeName: "", jobId: "", timeCreated: undefined, jobParams: undefined, status: "" };
}

export const JobRec = {
  encode(message: JobRec, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.pipeName !== "") {
      writer.uint32(18).string(message.pipeName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.timeCreated !== undefined) {
      Timestamp.encode(toTimestamp(message.timeCreated), writer.uint32(34).fork()).ldelim();
    }
    if (message.jobParams !== undefined) {
      Struct.encode(Struct.wrap(message.jobParams), writer.uint32(42).fork()).ldelim();
    }
    if (message.status !== "") {
      writer.uint32(50).string(message.status);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): JobRec {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseJobRec();
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

          message.timeCreated = fromTimestamp(Timestamp.decode(reader, reader.uint32()));
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.jobParams = Struct.unwrap(Struct.decode(reader, reader.uint32()));
          continue;
        case 6:
          if (tag !== 50) {
            break;
          }

          message.status = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): JobRec {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      pipeName: isSet(object.pipeName) ? globalThis.String(object.pipeName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      timeCreated: isSet(object.timeCreated) ? fromJsonTimestamp(object.timeCreated) : undefined,
      jobParams: isObject(object.jobParams) ? object.jobParams : undefined,
      status: isSet(object.status) ? globalThis.String(object.status) : "",
    };
  },

  toJSON(message: JobRec): unknown {
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
    if (message.timeCreated !== undefined) {
      obj.timeCreated = message.timeCreated.toISOString();
    }
    if (message.jobParams !== undefined) {
      obj.jobParams = message.jobParams;
    }
    if (message.status !== "") {
      obj.status = message.status;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<JobRec>, I>>(base?: I): JobRec {
    return JobRec.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<JobRec>, I>>(object: I): JobRec {
    const message = createBaseJobRec();
    message.projectId = object.projectId ?? "";
    message.pipeName = object.pipeName ?? "";
    message.jobId = object.jobId ?? "";
    message.timeCreated = object.timeCreated ?? undefined;
    message.jobParams = object.jobParams ?? undefined;
    message.status = object.status ?? "";
    return message;
  },
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
