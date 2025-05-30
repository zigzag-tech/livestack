// Code generated by protoc-gen-ts_proto. DO NOT EDIT.
// versions:
//   protoc-gen-ts_proto  v1.181.2
//   protoc               v5.27.0
// source: capacity.proto

/* eslint-disable */
import { type CallContext, type CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";
import { Empty } from "./google/protobuf/empty";

export const protobufPackage = "livestack";

export interface SpecCapacity {
  specName: string;
  capacity: number;
}

export interface CapacityLog {
  specCapacity: SpecCapacity[];
}

export interface RespondToCapacityLogMessage {
  projectUuid: string;
}

export interface ReportAsInstanceMessage {
  projectUuid: string;
  instanceId: string;
}

export interface SpecNameAndCapacity {
  specName: string;
  capacity: number;
}

export interface InstanceResponseToCapacityQueryMessage {
  correlationId: string;
  projectUuid: string;
  instanceId: string;
  specNameAndCapacity: SpecNameAndCapacity[];
}

export interface InstanceResponseToProvisionMessage {
  correlationId: string;
  projectUuid: string;
  instanceId: string;
  specName: string;
  /** status: 0: success, 1: partial success, 2: failed */
  status: number;
  numberOfWorkersStarted: number;
}

export interface ProvisionCommand {
  specName: string;
  numberOfWorkersNeeded: number;
}

export interface QueryCapacityCommand {
}

export interface NoCapacityWarning {
  specName: string;
}

export interface CommandToInstance {
  projectUuid: string;
  instanceId: string;
  correlationId: string;
  provision?: ProvisionCommand | undefined;
  queryCapacity?: QueryCapacityCommand | undefined;
  noCapacityWarning?: NoCapacityWarning | undefined;
}

function createBaseSpecCapacity(): SpecCapacity {
  return { specName: "", capacity: 0 };
}

export const SpecCapacity = {
  encode(message: SpecCapacity, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(10).string(message.specName);
    }
    if (message.capacity !== 0) {
      writer.uint32(16).int32(message.capacity);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): SpecCapacity {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseSpecCapacity();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 2:
          if (tag !== 16) {
            break;
          }

          message.capacity = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): SpecCapacity {
    return {
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      capacity: isSet(object.capacity) ? globalThis.Number(object.capacity) : 0,
    };
  },

  toJSON(message: SpecCapacity): unknown {
    const obj: any = {};
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.capacity !== 0) {
      obj.capacity = Math.round(message.capacity);
    }
    return obj;
  },

  create(base?: DeepPartial<SpecCapacity>): SpecCapacity {
    return SpecCapacity.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<SpecCapacity>): SpecCapacity {
    const message = createBaseSpecCapacity();
    message.specName = object.specName ?? "";
    message.capacity = object.capacity ?? 0;
    return message;
  },
};

function createBaseCapacityLog(): CapacityLog {
  return { specCapacity: [] };
}

export const CapacityLog = {
  encode(message: CapacityLog, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    for (const v of message.specCapacity) {
      SpecCapacity.encode(v!, writer.uint32(10).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): CapacityLog {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseCapacityLog();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.specCapacity.push(SpecCapacity.decode(reader, reader.uint32()));
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): CapacityLog {
    return {
      specCapacity: globalThis.Array.isArray(object?.specCapacity)
        ? object.specCapacity.map((e: any) => SpecCapacity.fromJSON(e))
        : [],
    };
  },

  toJSON(message: CapacityLog): unknown {
    const obj: any = {};
    if (message.specCapacity?.length) {
      obj.specCapacity = message.specCapacity.map((e) => SpecCapacity.toJSON(e));
    }
    return obj;
  },

  create(base?: DeepPartial<CapacityLog>): CapacityLog {
    return CapacityLog.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<CapacityLog>): CapacityLog {
    const message = createBaseCapacityLog();
    message.specCapacity = object.specCapacity?.map((e) => SpecCapacity.fromPartial(e)) || [];
    return message;
  },
};

function createBaseRespondToCapacityLogMessage(): RespondToCapacityLogMessage {
  return { projectUuid: "" };
}

export const RespondToCapacityLogMessage = {
  encode(message: RespondToCapacityLogMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): RespondToCapacityLogMessage {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseRespondToCapacityLogMessage();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.projectUuid = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): RespondToCapacityLogMessage {
    return { projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "" };
  },

  toJSON(message: RespondToCapacityLogMessage): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    return obj;
  },

  create(base?: DeepPartial<RespondToCapacityLogMessage>): RespondToCapacityLogMessage {
    return RespondToCapacityLogMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<RespondToCapacityLogMessage>): RespondToCapacityLogMessage {
    const message = createBaseRespondToCapacityLogMessage();
    message.projectUuid = object.projectUuid ?? "";
    return message;
  },
};

function createBaseReportAsInstanceMessage(): ReportAsInstanceMessage {
  return { projectUuid: "", instanceId: "" };
}

export const ReportAsInstanceMessage = {
  encode(message: ReportAsInstanceMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.instanceId !== "") {
      writer.uint32(18).string(message.instanceId);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ReportAsInstanceMessage {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseReportAsInstanceMessage();
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

          message.instanceId = reader.string();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ReportAsInstanceMessage {
    return {
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
    };
  },

  toJSON(message: ReportAsInstanceMessage): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    return obj;
  },

  create(base?: DeepPartial<ReportAsInstanceMessage>): ReportAsInstanceMessage {
    return ReportAsInstanceMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ReportAsInstanceMessage>): ReportAsInstanceMessage {
    const message = createBaseReportAsInstanceMessage();
    message.projectUuid = object.projectUuid ?? "";
    message.instanceId = object.instanceId ?? "";
    return message;
  },
};

function createBaseSpecNameAndCapacity(): SpecNameAndCapacity {
  return { specName: "", capacity: 0 };
}

export const SpecNameAndCapacity = {
  encode(message: SpecNameAndCapacity, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(10).string(message.specName);
    }
    if (message.capacity !== 0) {
      writer.uint32(16).int32(message.capacity);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): SpecNameAndCapacity {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseSpecNameAndCapacity();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 2:
          if (tag !== 16) {
            break;
          }

          message.capacity = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): SpecNameAndCapacity {
    return {
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      capacity: isSet(object.capacity) ? globalThis.Number(object.capacity) : 0,
    };
  },

  toJSON(message: SpecNameAndCapacity): unknown {
    const obj: any = {};
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.capacity !== 0) {
      obj.capacity = Math.round(message.capacity);
    }
    return obj;
  },

  create(base?: DeepPartial<SpecNameAndCapacity>): SpecNameAndCapacity {
    return SpecNameAndCapacity.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<SpecNameAndCapacity>): SpecNameAndCapacity {
    const message = createBaseSpecNameAndCapacity();
    message.specName = object.specName ?? "";
    message.capacity = object.capacity ?? 0;
    return message;
  },
};

function createBaseInstanceResponseToCapacityQueryMessage(): InstanceResponseToCapacityQueryMessage {
  return { correlationId: "", projectUuid: "", instanceId: "", specNameAndCapacity: [] };
}

export const InstanceResponseToCapacityQueryMessage = {
  encode(message: InstanceResponseToCapacityQueryMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.correlationId !== "") {
      writer.uint32(10).string(message.correlationId);
    }
    if (message.projectUuid !== "") {
      writer.uint32(18).string(message.projectUuid);
    }
    if (message.instanceId !== "") {
      writer.uint32(26).string(message.instanceId);
    }
    for (const v of message.specNameAndCapacity) {
      SpecNameAndCapacity.encode(v!, writer.uint32(34).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): InstanceResponseToCapacityQueryMessage {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseInstanceResponseToCapacityQueryMessage();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.correlationId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.projectUuid = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.instanceId = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.specNameAndCapacity.push(SpecNameAndCapacity.decode(reader, reader.uint32()));
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): InstanceResponseToCapacityQueryMessage {
    return {
      correlationId: isSet(object.correlationId) ? globalThis.String(object.correlationId) : "",
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      specNameAndCapacity: globalThis.Array.isArray(object?.specNameAndCapacity)
        ? object.specNameAndCapacity.map((e: any) => SpecNameAndCapacity.fromJSON(e))
        : [],
    };
  },

  toJSON(message: InstanceResponseToCapacityQueryMessage): unknown {
    const obj: any = {};
    if (message.correlationId !== "") {
      obj.correlationId = message.correlationId;
    }
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.specNameAndCapacity?.length) {
      obj.specNameAndCapacity = message.specNameAndCapacity.map((e) => SpecNameAndCapacity.toJSON(e));
    }
    return obj;
  },

  create(base?: DeepPartial<InstanceResponseToCapacityQueryMessage>): InstanceResponseToCapacityQueryMessage {
    return InstanceResponseToCapacityQueryMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<InstanceResponseToCapacityQueryMessage>): InstanceResponseToCapacityQueryMessage {
    const message = createBaseInstanceResponseToCapacityQueryMessage();
    message.correlationId = object.correlationId ?? "";
    message.projectUuid = object.projectUuid ?? "";
    message.instanceId = object.instanceId ?? "";
    message.specNameAndCapacity = object.specNameAndCapacity?.map((e) => SpecNameAndCapacity.fromPartial(e)) || [];
    return message;
  },
};

function createBaseInstanceResponseToProvisionMessage(): InstanceResponseToProvisionMessage {
  return { correlationId: "", projectUuid: "", instanceId: "", specName: "", status: 0, numberOfWorkersStarted: 0 };
}

export const InstanceResponseToProvisionMessage = {
  encode(message: InstanceResponseToProvisionMessage, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.correlationId !== "") {
      writer.uint32(10).string(message.correlationId);
    }
    if (message.projectUuid !== "") {
      writer.uint32(18).string(message.projectUuid);
    }
    if (message.instanceId !== "") {
      writer.uint32(26).string(message.instanceId);
    }
    if (message.specName !== "") {
      writer.uint32(34).string(message.specName);
    }
    if (message.status !== 0) {
      writer.uint32(40).int32(message.status);
    }
    if (message.numberOfWorkersStarted !== 0) {
      writer.uint32(48).int32(message.numberOfWorkersStarted);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): InstanceResponseToProvisionMessage {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseInstanceResponseToProvisionMessage();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.correlationId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.projectUuid = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.instanceId = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 5:
          if (tag !== 40) {
            break;
          }

          message.status = reader.int32();
          continue;
        case 6:
          if (tag !== 48) {
            break;
          }

          message.numberOfWorkersStarted = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): InstanceResponseToProvisionMessage {
    return {
      correlationId: isSet(object.correlationId) ? globalThis.String(object.correlationId) : "",
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      status: isSet(object.status) ? globalThis.Number(object.status) : 0,
      numberOfWorkersStarted: isSet(object.numberOfWorkersStarted)
        ? globalThis.Number(object.numberOfWorkersStarted)
        : 0,
    };
  },

  toJSON(message: InstanceResponseToProvisionMessage): unknown {
    const obj: any = {};
    if (message.correlationId !== "") {
      obj.correlationId = message.correlationId;
    }
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.status !== 0) {
      obj.status = Math.round(message.status);
    }
    if (message.numberOfWorkersStarted !== 0) {
      obj.numberOfWorkersStarted = Math.round(message.numberOfWorkersStarted);
    }
    return obj;
  },

  create(base?: DeepPartial<InstanceResponseToProvisionMessage>): InstanceResponseToProvisionMessage {
    return InstanceResponseToProvisionMessage.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<InstanceResponseToProvisionMessage>): InstanceResponseToProvisionMessage {
    const message = createBaseInstanceResponseToProvisionMessage();
    message.correlationId = object.correlationId ?? "";
    message.projectUuid = object.projectUuid ?? "";
    message.instanceId = object.instanceId ?? "";
    message.specName = object.specName ?? "";
    message.status = object.status ?? 0;
    message.numberOfWorkersStarted = object.numberOfWorkersStarted ?? 0;
    return message;
  },
};

function createBaseProvisionCommand(): ProvisionCommand {
  return { specName: "", numberOfWorkersNeeded: 0 };
}

export const ProvisionCommand = {
  encode(message: ProvisionCommand, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(10).string(message.specName);
    }
    if (message.numberOfWorkersNeeded !== 0) {
      writer.uint32(16).int32(message.numberOfWorkersNeeded);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ProvisionCommand {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseProvisionCommand();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 2:
          if (tag !== 16) {
            break;
          }

          message.numberOfWorkersNeeded = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ProvisionCommand {
    return {
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      numberOfWorkersNeeded: isSet(object.numberOfWorkersNeeded) ? globalThis.Number(object.numberOfWorkersNeeded) : 0,
    };
  },

  toJSON(message: ProvisionCommand): unknown {
    const obj: any = {};
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.numberOfWorkersNeeded !== 0) {
      obj.numberOfWorkersNeeded = Math.round(message.numberOfWorkersNeeded);
    }
    return obj;
  },

  create(base?: DeepPartial<ProvisionCommand>): ProvisionCommand {
    return ProvisionCommand.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ProvisionCommand>): ProvisionCommand {
    const message = createBaseProvisionCommand();
    message.specName = object.specName ?? "";
    message.numberOfWorkersNeeded = object.numberOfWorkersNeeded ?? 0;
    return message;
  },
};

function createBaseQueryCapacityCommand(): QueryCapacityCommand {
  return {};
}

export const QueryCapacityCommand = {
  encode(_: QueryCapacityCommand, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): QueryCapacityCommand {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseQueryCapacityCommand();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(_: any): QueryCapacityCommand {
    return {};
  },

  toJSON(_: QueryCapacityCommand): unknown {
    const obj: any = {};
    return obj;
  },

  create(base?: DeepPartial<QueryCapacityCommand>): QueryCapacityCommand {
    return QueryCapacityCommand.fromPartial(base ?? {});
  },
  fromPartial(_: DeepPartial<QueryCapacityCommand>): QueryCapacityCommand {
    const message = createBaseQueryCapacityCommand();
    return message;
  },
};

function createBaseNoCapacityWarning(): NoCapacityWarning {
  return { specName: "" };
}

export const NoCapacityWarning = {
  encode(message: NoCapacityWarning, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(26).string(message.specName);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): NoCapacityWarning {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseNoCapacityWarning();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 3:
          if (tag !== 26) {
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

  fromJSON(object: any): NoCapacityWarning {
    return { specName: isSet(object.specName) ? globalThis.String(object.specName) : "" };
  },

  toJSON(message: NoCapacityWarning): unknown {
    const obj: any = {};
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    return obj;
  },

  create(base?: DeepPartial<NoCapacityWarning>): NoCapacityWarning {
    return NoCapacityWarning.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<NoCapacityWarning>): NoCapacityWarning {
    const message = createBaseNoCapacityWarning();
    message.specName = object.specName ?? "";
    return message;
  },
};

function createBaseCommandToInstance(): CommandToInstance {
  return {
    projectUuid: "",
    instanceId: "",
    correlationId: "",
    provision: undefined,
    queryCapacity: undefined,
    noCapacityWarning: undefined,
  };
}

export const CommandToInstance = {
  encode(message: CommandToInstance, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectUuid !== "") {
      writer.uint32(10).string(message.projectUuid);
    }
    if (message.instanceId !== "") {
      writer.uint32(18).string(message.instanceId);
    }
    if (message.correlationId !== "") {
      writer.uint32(26).string(message.correlationId);
    }
    if (message.provision !== undefined) {
      ProvisionCommand.encode(message.provision, writer.uint32(34).fork()).ldelim();
    }
    if (message.queryCapacity !== undefined) {
      QueryCapacityCommand.encode(message.queryCapacity, writer.uint32(42).fork()).ldelim();
    }
    if (message.noCapacityWarning !== undefined) {
      NoCapacityWarning.encode(message.noCapacityWarning, writer.uint32(50).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): CommandToInstance {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseCommandToInstance();
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

          message.instanceId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.correlationId = reader.string();
          continue;
        case 4:
          if (tag !== 34) {
            break;
          }

          message.provision = ProvisionCommand.decode(reader, reader.uint32());
          continue;
        case 5:
          if (tag !== 42) {
            break;
          }

          message.queryCapacity = QueryCapacityCommand.decode(reader, reader.uint32());
          continue;
        case 6:
          if (tag !== 50) {
            break;
          }

          message.noCapacityWarning = NoCapacityWarning.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): CommandToInstance {
    return {
      projectUuid: isSet(object.projectUuid) ? globalThis.String(object.projectUuid) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      correlationId: isSet(object.correlationId) ? globalThis.String(object.correlationId) : "",
      provision: isSet(object.provision) ? ProvisionCommand.fromJSON(object.provision) : undefined,
      queryCapacity: isSet(object.queryCapacity) ? QueryCapacityCommand.fromJSON(object.queryCapacity) : undefined,
      noCapacityWarning: isSet(object.noCapacityWarning)
        ? NoCapacityWarning.fromJSON(object.noCapacityWarning)
        : undefined,
    };
  },

  toJSON(message: CommandToInstance): unknown {
    const obj: any = {};
    if (message.projectUuid !== "") {
      obj.projectUuid = message.projectUuid;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.correlationId !== "") {
      obj.correlationId = message.correlationId;
    }
    if (message.provision !== undefined) {
      obj.provision = ProvisionCommand.toJSON(message.provision);
    }
    if (message.queryCapacity !== undefined) {
      obj.queryCapacity = QueryCapacityCommand.toJSON(message.queryCapacity);
    }
    if (message.noCapacityWarning !== undefined) {
      obj.noCapacityWarning = NoCapacityWarning.toJSON(message.noCapacityWarning);
    }
    return obj;
  },

  create(base?: DeepPartial<CommandToInstance>): CommandToInstance {
    return CommandToInstance.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<CommandToInstance>): CommandToInstance {
    const message = createBaseCommandToInstance();
    message.projectUuid = object.projectUuid ?? "";
    message.instanceId = object.instanceId ?? "";
    message.correlationId = object.correlationId ?? "";
    message.provision = (object.provision !== undefined && object.provision !== null)
      ? ProvisionCommand.fromPartial(object.provision)
      : undefined;
    message.queryCapacity = (object.queryCapacity !== undefined && object.queryCapacity !== null)
      ? QueryCapacityCommand.fromPartial(object.queryCapacity)
      : undefined;
    message.noCapacityWarning = (object.noCapacityWarning !== undefined && object.noCapacityWarning !== null)
      ? NoCapacityWarning.fromPartial(object.noCapacityWarning)
      : undefined;
    return message;
  },
};

export type CacapcityServiceDefinition = typeof CacapcityServiceDefinition;
export const CacapcityServiceDefinition = {
  name: "CacapcityService",
  fullName: "livestack.CacapcityService",
  methods: {
    reportAsInstance: {
      name: "ReportAsInstance",
      requestType: ReportAsInstanceMessage,
      requestStream: false,
      responseType: CommandToInstance,
      responseStream: true,
      options: {},
    },
    respondToCapacityQuery: {
      name: "RespondToCapacityQuery",
      requestType: InstanceResponseToCapacityQueryMessage,
      requestStream: false,
      responseType: Empty,
      responseStream: false,
      options: {},
    },
    respondToProvision: {
      name: "RespondToProvision",
      requestType: InstanceResponseToProvisionMessage,
      requestStream: false,
      responseType: Empty,
      responseStream: false,
      options: {},
    },
    respondToCapacityLog: {
      name: "RespondToCapacityLog",
      requestType: RespondToCapacityLogMessage,
      requestStream: false,
      responseType: CapacityLog,
      responseStream: false,
      options: {},
    },
  },
} as const;

export interface CacapcityServiceImplementation<CallContextExt = {}> {
  reportAsInstance(
    request: ReportAsInstanceMessage,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<CommandToInstance>>;
  respondToCapacityQuery(
    request: InstanceResponseToCapacityQueryMessage,
    context: CallContext & CallContextExt,
  ): Promise<DeepPartial<Empty>>;
  respondToProvision(
    request: InstanceResponseToProvisionMessage,
    context: CallContext & CallContextExt,
  ): Promise<DeepPartial<Empty>>;
  respondToCapacityLog(
    request: RespondToCapacityLogMessage,
    context: CallContext & CallContextExt,
  ): Promise<DeepPartial<CapacityLog>>;
}

export interface CacapcityServiceClient<CallOptionsExt = {}> {
  reportAsInstance(
    request: DeepPartial<ReportAsInstanceMessage>,
    options?: CallOptions & CallOptionsExt,
  ): AsyncIterable<CommandToInstance>;
  respondToCapacityQuery(
    request: DeepPartial<InstanceResponseToCapacityQueryMessage>,
    options?: CallOptions & CallOptionsExt,
  ): Promise<Empty>;
  respondToProvision(
    request: DeepPartial<InstanceResponseToProvisionMessage>,
    options?: CallOptions & CallOptionsExt,
  ): Promise<Empty>;
  respondToCapacityLog(
    request: DeepPartial<RespondToCapacityLogMessage>,
    options?: CallOptions & CallOptionsExt,
  ): Promise<CapacityLog>;
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
