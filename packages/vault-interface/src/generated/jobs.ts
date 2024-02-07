/* eslint-disable */
import _m0 from "protobufjs/minimal";
import { Empty } from "./google/protobuf/empty";
import { Struct } from "./google/protobuf/struct";
import { Timestamp } from "./google/protobuf/timestamp";

export const protobufPackage = "livestack";

export interface GetJobRecRequest {
  project_id: string;
  pipe_name: string;
  job_id: string;
}

export interface GetJobRecResponse {
  /** The actual response data */
  job_data?:
    | JobRec
    | undefined;
  /** Signal that the response is null */
  null_response?: Empty | undefined;
}

export interface JobRec {
  project_id: string;
  /** Align with jobs.ts */
  spec_name: string;
  job_id: string;
  time_created: Date | undefined;
  job_params: { [key: string]: any } | undefined;
}

function createBaseGetJobRecRequest(): GetJobRecRequest {
  return { project_id: "", pipe_name: "", job_id: "" };
}

export const GetJobRecRequest = {
  encode(message: GetJobRecRequest, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.project_id !== "") {
      writer.uint32(10).string(message.project_id);
    }
    if (message.pipe_name !== "") {
      writer.uint32(18).string(message.pipe_name);
    }
    if (message.job_id !== "") {
      writer.uint32(26).string(message.job_id);
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

          message.project_id = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.pipe_name = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.job_id = reader.string();
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
      project_id: isSet(object.project_id) ? globalThis.String(object.project_id) : "",
      pipe_name: isSet(object.pipe_name) ? globalThis.String(object.pipe_name) : "",
      job_id: isSet(object.job_id) ? globalThis.String(object.job_id) : "",
    };
  },

  toJSON(message: GetJobRecRequest): unknown {
    const obj: any = {};
    if (message.project_id !== "") {
      obj.project_id = message.project_id;
    }
    if (message.pipe_name !== "") {
      obj.pipe_name = message.pipe_name;
    }
    if (message.job_id !== "") {
      obj.job_id = message.job_id;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetJobRecRequest>, I>>(base?: I): GetJobRecRequest {
    return GetJobRecRequest.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobRecRequest>, I>>(object: I): GetJobRecRequest {
    const message = createBaseGetJobRecRequest();
    message.project_id = object.project_id ?? "";
    message.pipe_name = object.pipe_name ?? "";
    message.job_id = object.job_id ?? "";
    return message;
  },
};

function createBaseGetJobRecResponse(): GetJobRecResponse {
  return { job_data: undefined, null_response: undefined };
}

export const GetJobRecResponse = {
  encode(message: GetJobRecResponse, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.job_data !== undefined) {
      JobRec.encode(message.job_data, writer.uint32(10).fork()).ldelim();
    }
    if (message.null_response !== undefined) {
      Empty.encode(message.null_response, writer.uint32(18).fork()).ldelim();
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

          message.job_data = JobRec.decode(reader, reader.uint32());
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

  fromJSON(object: any): GetJobRecResponse {
    return {
      job_data: isSet(object.job_data) ? JobRec.fromJSON(object.job_data) : undefined,
      null_response: isSet(object.null_response) ? Empty.fromJSON(object.null_response) : undefined,
    };
  },

  toJSON(message: GetJobRecResponse): unknown {
    const obj: any = {};
    if (message.job_data !== undefined) {
      obj.job_data = JobRec.toJSON(message.job_data);
    }
    if (message.null_response !== undefined) {
      obj.null_response = Empty.toJSON(message.null_response);
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<GetJobRecResponse>, I>>(base?: I): GetJobRecResponse {
    return GetJobRecResponse.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<GetJobRecResponse>, I>>(object: I): GetJobRecResponse {
    const message = createBaseGetJobRecResponse();
    message.job_data = (object.job_data !== undefined && object.job_data !== null)
      ? JobRec.fromPartial(object.job_data)
      : undefined;
    message.null_response = (object.null_response !== undefined && object.null_response !== null)
      ? Empty.fromPartial(object.null_response)
      : undefined;
    return message;
  },
};

function createBaseJobRec(): JobRec {
  return { project_id: "", spec_name: "", job_id: "", time_created: undefined, job_params: undefined };
}

export const JobRec = {
  encode(message: JobRec, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.project_id !== "") {
      writer.uint32(10).string(message.project_id);
    }
    if (message.spec_name !== "") {
      writer.uint32(18).string(message.spec_name);
    }
    if (message.job_id !== "") {
      writer.uint32(26).string(message.job_id);
    }
    if (message.time_created !== undefined) {
      Timestamp.encode(toTimestamp(message.time_created), writer.uint32(34).fork()).ldelim();
    }
    if (message.job_params !== undefined) {
      Struct.encode(Struct.wrap(message.job_params), writer.uint32(42).fork()).ldelim();
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

          message.project_id = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.spec_name = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.job_id = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.time_created = fromTimestamp(Timestamp.decode(reader, reader.uint32()));
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.job_params = Struct.unwrap(Struct.decode(reader, reader.uint32()));
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
      project_id: isSet(object.project_id) ? globalThis.String(object.project_id) : "",
      spec_name: isSet(object.spec_name) ? globalThis.String(object.spec_name) : "",
      job_id: isSet(object.job_id) ? globalThis.String(object.job_id) : "",
      time_created: isSet(object.time_created) ? fromJsonTimestamp(object.time_created) : undefined,
      job_params: isObject(object.job_params) ? object.job_params : undefined,
    };
  },

  toJSON(message: JobRec): unknown {
    const obj: any = {};
    if (message.project_id !== "") {
      obj.project_id = message.project_id;
    }
    if (message.spec_name !== "") {
      obj.spec_name = message.spec_name;
    }
    if (message.job_id !== "") {
      obj.job_id = message.job_id;
    }
    if (message.time_created !== undefined) {
      obj.time_created = message.time_created.toISOString();
    }
    if (message.job_params !== undefined) {
      obj.job_params = message.job_params;
    }
    return obj;
  },

  create<I extends Exact<DeepPartial<JobRec>, I>>(base?: I): JobRec {
    return JobRec.fromPartial(base ?? ({} as any));
  },
  fromPartial<I extends Exact<DeepPartial<JobRec>, I>>(object: I): JobRec {
    const message = createBaseJobRec();
    message.project_id = object.project_id ?? "";
    message.spec_name = object.spec_name ?? "";
    message.job_id = object.job_id ?? "";
    message.time_created = object.time_created ?? undefined;
    message.job_params = object.job_params ?? undefined;
    return message;
  },
};

export interface jobs {
  GetJobRec(request: GetJobRecRequest): Promise<GetJobRecResponse>;
}

export const jobsServiceName = "livestack.jobs";
export class jobsClientImpl implements jobs {
  private readonly rpc: Rpc;
  private readonly service: string;
  constructor(rpc: Rpc, opts?: { service?: string }) {
    this.service = opts?.service || jobsServiceName;
    this.rpc = rpc;
    this.GetJobRec = this.GetJobRec.bind(this);
  }
  GetJobRec(request: GetJobRecRequest): Promise<GetJobRecResponse> {
    const data = GetJobRecRequest.encode(request).finish();
    const promise = this.rpc.request(this.service, "GetJobRec", data);
    return promise.then((data) => GetJobRecResponse.decode(_m0.Reader.create(data)));
  }
}

interface Rpc {
  request(service: string, method: string, data: Uint8Array): Promise<Uint8Array>;
}

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
