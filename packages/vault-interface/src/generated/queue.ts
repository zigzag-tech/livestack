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

export interface SignUp {
  projectId: string;
  specName: string;
}

export interface WorkerStopped {
  projectId: string;
  specName: string;
}

export interface ProgressUpdate {
  projectId: string;
  specName: string;
  jobId: string;
  progress: number;
}

export interface JobCompleted {
  projectId: string;
  specName: string;
  jobId: string;
}

export interface JobFailed {
  projectId: string;
  specName: string;
  jobId: string;
  errorStr: string;
}

export interface FromWorker {
  signUp?: SignUp | undefined;
  progressUpdate?: ProgressUpdate | undefined;
  jobCompleted?: JobCompleted | undefined;
  jobFailed?: JobFailed | undefined;
  workerStopped?: WorkerStopped | undefined;
}

export interface ToWorker {
  job: QueueJob | undefined;
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

function createBaseSignUp(): SignUp {
  return { projectId: "", specName: "" };
}

export const SignUp = {
  encode(message: SignUp, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): SignUp {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseSignUp();
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
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): SignUp {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
    };
  },

  toJSON(message: SignUp): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    return obj;
  },

  create(base?: DeepPartial<SignUp>): SignUp {
    return SignUp.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<SignUp>): SignUp {
    const message = createBaseSignUp();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    return message;
  },
};

function createBaseWorkerStopped(): WorkerStopped {
  return { projectId: "", specName: "" };
}

export const WorkerStopped = {
  encode(message: WorkerStopped, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): WorkerStopped {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseWorkerStopped();
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
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): WorkerStopped {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
    };
  },

  toJSON(message: WorkerStopped): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    return obj;
  },

  create(base?: DeepPartial<WorkerStopped>): WorkerStopped {
    return WorkerStopped.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<WorkerStopped>): WorkerStopped {
    const message = createBaseWorkerStopped();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    return message;
  },
};

function createBaseProgressUpdate(): ProgressUpdate {
  return { projectId: "", specName: "", jobId: "", progress: 0 };
}

export const ProgressUpdate = {
  encode(message: ProgressUpdate, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.progress !== 0) {
      writer.uint32(32).int32(message.progress);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ProgressUpdate {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseProgressUpdate();
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
          if (tag !== 32) {
            break;
          }

          message.progress = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ProgressUpdate {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      progress: isSet(object.progress) ? globalThis.Number(object.progress) : 0,
    };
  },

  toJSON(message: ProgressUpdate): unknown {
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
    if (message.progress !== 0) {
      obj.progress = Math.round(message.progress);
    }
    return obj;
  },

  create(base?: DeepPartial<ProgressUpdate>): ProgressUpdate {
    return ProgressUpdate.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ProgressUpdate>): ProgressUpdate {
    const message = createBaseProgressUpdate();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    message.jobId = object.jobId ?? "";
    message.progress = object.progress ?? 0;
    return message;
  },
};

function createBaseJobCompleted(): JobCompleted {
  return { projectId: "", specName: "", jobId: "" };
}

export const JobCompleted = {
  encode(message: JobCompleted, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): JobCompleted {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseJobCompleted();
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
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): JobCompleted {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
    };
  },

  toJSON(message: JobCompleted): unknown {
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
    return obj;
  },

  create(base?: DeepPartial<JobCompleted>): JobCompleted {
    return JobCompleted.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<JobCompleted>): JobCompleted {
    const message = createBaseJobCompleted();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    message.jobId = object.jobId ?? "";
    return message;
  },
};

function createBaseJobFailed(): JobFailed {
  return { projectId: "", specName: "", jobId: "", errorStr: "" };
}

export const JobFailed = {
  encode(message: JobFailed, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.jobId !== "") {
      writer.uint32(26).string(message.jobId);
    }
    if (message.errorStr !== "") {
      writer.uint32(34).string(message.errorStr);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): JobFailed {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseJobFailed();
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

          message.errorStr = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): JobFailed {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      jobId: isSet(object.jobId) ? globalThis.String(object.jobId) : "",
      errorStr: isSet(object.errorStr) ? globalThis.String(object.errorStr) : "",
    };
  },

  toJSON(message: JobFailed): unknown {
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
    if (message.errorStr !== "") {
      obj.errorStr = message.errorStr;
    }
    return obj;
  },

  create(base?: DeepPartial<JobFailed>): JobFailed {
    return JobFailed.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<JobFailed>): JobFailed {
    const message = createBaseJobFailed();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    message.jobId = object.jobId ?? "";
    message.errorStr = object.errorStr ?? "";
    return message;
  },
};

function createBaseFromWorker(): FromWorker {
  return {
    signUp: undefined,
    progressUpdate: undefined,
    jobCompleted: undefined,
    jobFailed: undefined,
    workerStopped: undefined,
  };
}

export const FromWorker = {
  encode(message: FromWorker, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.signUp !== undefined) {
      SignUp.encode(message.signUp, writer.uint32(10).fork()).ldelim();
    }
    if (message.progressUpdate !== undefined) {
      ProgressUpdate.encode(message.progressUpdate, writer.uint32(18).fork()).ldelim();
    }
    if (message.jobCompleted !== undefined) {
      JobCompleted.encode(message.jobCompleted, writer.uint32(26).fork()).ldelim();
    }
    if (message.jobFailed !== undefined) {
      JobFailed.encode(message.jobFailed, writer.uint32(34).fork()).ldelim();
    }
    if (message.workerStopped !== undefined) {
      WorkerStopped.encode(message.workerStopped, writer.uint32(42).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): FromWorker {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseFromWorker();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.signUp = SignUp.decode(reader, reader.uint32());
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.progressUpdate = ProgressUpdate.decode(reader, reader.uint32());
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.jobCompleted = JobCompleted.decode(reader, reader.uint32());
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.jobFailed = JobFailed.decode(reader, reader.uint32());
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.workerStopped = WorkerStopped.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): FromWorker {
    return {
      signUp: isSet(object.signUp) ? SignUp.fromJSON(object.signUp) : undefined,
      progressUpdate: isSet(object.progressUpdate) ? ProgressUpdate.fromJSON(object.progressUpdate) : undefined,
      jobCompleted: isSet(object.jobCompleted) ? JobCompleted.fromJSON(object.jobCompleted) : undefined,
      jobFailed: isSet(object.jobFailed) ? JobFailed.fromJSON(object.jobFailed) : undefined,
      workerStopped: isSet(object.workerStopped) ? WorkerStopped.fromJSON(object.workerStopped) : undefined,
    };
  },

  toJSON(message: FromWorker): unknown {
    const obj: any = {};
    if (message.signUp !== undefined) {
      obj.signUp = SignUp.toJSON(message.signUp);
    }
    if (message.progressUpdate !== undefined) {
      obj.progressUpdate = ProgressUpdate.toJSON(message.progressUpdate);
    }
    if (message.jobCompleted !== undefined) {
      obj.jobCompleted = JobCompleted.toJSON(message.jobCompleted);
    }
    if (message.jobFailed !== undefined) {
      obj.jobFailed = JobFailed.toJSON(message.jobFailed);
    }
    if (message.workerStopped !== undefined) {
      obj.workerStopped = WorkerStopped.toJSON(message.workerStopped);
    }
    return obj;
  },

  create(base?: DeepPartial<FromWorker>): FromWorker {
    return FromWorker.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<FromWorker>): FromWorker {
    const message = createBaseFromWorker();
    message.signUp = (object.signUp !== undefined && object.signUp !== null)
      ? SignUp.fromPartial(object.signUp)
      : undefined;
    message.progressUpdate = (object.progressUpdate !== undefined && object.progressUpdate !== null)
      ? ProgressUpdate.fromPartial(object.progressUpdate)
      : undefined;
    message.jobCompleted = (object.jobCompleted !== undefined && object.jobCompleted !== null)
      ? JobCompleted.fromPartial(object.jobCompleted)
      : undefined;
    message.jobFailed = (object.jobFailed !== undefined && object.jobFailed !== null)
      ? JobFailed.fromPartial(object.jobFailed)
      : undefined;
    message.workerStopped = (object.workerStopped !== undefined && object.workerStopped !== null)
      ? WorkerStopped.fromPartial(object.workerStopped)
      : undefined;
    return message;
  },
};

function createBaseToWorker(): ToWorker {
  return { job: undefined };
}

export const ToWorker = {
  encode(message: ToWorker, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.job !== undefined) {
      QueueJob.encode(message.job, writer.uint32(10).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ToWorker {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseToWorker();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.job = QueueJob.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ToWorker {
    return { job: isSet(object.job) ? QueueJob.fromJSON(object.job) : undefined };
  },

  toJSON(message: ToWorker): unknown {
    const obj: any = {};
    if (message.job !== undefined) {
      obj.job = QueueJob.toJSON(message.job);
    }
    return obj;
  },

  create(base?: DeepPartial<ToWorker>): ToWorker {
    return ToWorker.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ToWorker>): ToWorker {
    const message = createBaseToWorker();
    message.job = (object.job !== undefined && object.job !== null) ? QueueJob.fromPartial(object.job) : undefined;
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
    workerReportDuty: {
      name: "WorkerReportDuty",
      requestType: FromWorker,
      requestStream: true,
      responseType: ToWorker,
      responseStream: true,
      options: {},
    },
  },
} as const;

export interface QueueServiceImplementation<CallContextExt = {}> {
  addJob(request: QueueJob, context: CallContext & CallContextExt): Promise<DeepPartial<Empty>>;
  workerReportDuty(
    request: AsyncIterable<FromWorker>,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<ToWorker>>;
}

export interface QueueServiceClient<CallOptionsExt = {}> {
  addJob(request: DeepPartial<QueueJob>, options?: CallOptions & CallOptionsExt): Promise<Empty>;
  workerReportDuty(
    request: AsyncIterable<DeepPartial<FromWorker>>,
    options?: CallOptions & CallOptionsExt,
  ): AsyncIterable<ToWorker>;
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