/* eslint-disable */
import type { CallContext, CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";

export const protobufPackage = "livestack";

export interface ReportSpecAvailability {
  specName: string;
  maxCapacity: number;
}

export interface FromInstance {
  projectId: string;
  instanceId: string;
  reportSpecAvailability?: ReportSpecAvailability | undefined;
}

export interface Provision {
  projectId: string;
  specName: string;
  numberOfWorkersNeeded: number;
}

export interface CommandToInstance {
  projectId: string;
  instanceId: string;
  provision?: Provision | undefined;
}

function createBaseReportSpecAvailability(): ReportSpecAvailability {
  return { specName: "", maxCapacity: 0 };
}

export const ReportSpecAvailability = {
  encode(message: ReportSpecAvailability, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(26).string(message.specName);
    }
    if (message.maxCapacity !== 0) {
      writer.uint32(32).int32(message.maxCapacity);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ReportSpecAvailability {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseReportSpecAvailability();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 3:
          if (tag !== 26) {
            break;
          }

          message.specName = reader.string();
          continue;
        case 4:
          if (tag !== 32) {
            break;
          }

          message.maxCapacity = reader.int32();
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): ReportSpecAvailability {
    return {
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      maxCapacity: isSet(object.maxCapacity) ? globalThis.Number(object.maxCapacity) : 0,
    };
  },

  toJSON(message: ReportSpecAvailability): unknown {
    const obj: any = {};
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.maxCapacity !== 0) {
      obj.maxCapacity = Math.round(message.maxCapacity);
    }
    return obj;
  },

  create(base?: DeepPartial<ReportSpecAvailability>): ReportSpecAvailability {
    return ReportSpecAvailability.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ReportSpecAvailability>): ReportSpecAvailability {
    const message = createBaseReportSpecAvailability();
    message.specName = object.specName ?? "";
    message.maxCapacity = object.maxCapacity ?? 0;
    return message;
  },
};

function createBaseFromInstance(): FromInstance {
  return { projectId: "", instanceId: "", reportSpecAvailability: undefined };
}

export const FromInstance = {
  encode(message: FromInstance, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.instanceId !== "") {
      writer.uint32(18).string(message.instanceId);
    }
    if (message.reportSpecAvailability !== undefined) {
      ReportSpecAvailability.encode(message.reportSpecAvailability, writer.uint32(26).fork()).ldelim();
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): FromInstance {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseFromInstance();
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

          message.instanceId = reader.string();
          continue;
        case 3:
          if (tag !== 26) {
            break;
          }

          message.reportSpecAvailability = ReportSpecAvailability.decode(reader, reader.uint32());
          continue;
      }
      if ((tag & 7) === 4 || tag === 0) {
        break;
      }
      reader.skipType(tag & 7);
    }
    return message;
  },

  fromJSON(object: any): FromInstance {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      reportSpecAvailability: isSet(object.reportSpecAvailability)
        ? ReportSpecAvailability.fromJSON(object.reportSpecAvailability)
        : undefined,
    };
  },

  toJSON(message: FromInstance): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.reportSpecAvailability !== undefined) {
      obj.reportSpecAvailability = ReportSpecAvailability.toJSON(message.reportSpecAvailability);
    }
    return obj;
  },

  create(base?: DeepPartial<FromInstance>): FromInstance {
    return FromInstance.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<FromInstance>): FromInstance {
    const message = createBaseFromInstance();
    message.projectId = object.projectId ?? "";
    message.instanceId = object.instanceId ?? "";
    message.reportSpecAvailability =
      (object.reportSpecAvailability !== undefined && object.reportSpecAvailability !== null)
        ? ReportSpecAvailability.fromPartial(object.reportSpecAvailability)
        : undefined;
    return message;
  },
};

function createBaseProvision(): Provision {
  return { projectId: "", specName: "", numberOfWorkersNeeded: 0 };
}

export const Provision = {
  encode(message: Provision, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.numberOfWorkersNeeded !== 0) {
      writer.uint32(24).int32(message.numberOfWorkersNeeded);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): Provision {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseProvision();
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
          if (tag !== 24) {
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

  fromJSON(object: any): Provision {
    return {
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      numberOfWorkersNeeded: isSet(object.numberOfWorkersNeeded) ? globalThis.Number(object.numberOfWorkersNeeded) : 0,
    };
  },

  toJSON(message: Provision): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.numberOfWorkersNeeded !== 0) {
      obj.numberOfWorkersNeeded = Math.round(message.numberOfWorkersNeeded);
    }
    return obj;
  },

  create(base?: DeepPartial<Provision>): Provision {
    return Provision.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<Provision>): Provision {
    const message = createBaseProvision();
    message.projectId = object.projectId ?? "";
    message.specName = object.specName ?? "";
    message.numberOfWorkersNeeded = object.numberOfWorkersNeeded ?? 0;
    return message;
  },
};

function createBaseCommandToInstance(): CommandToInstance {
  return { projectId: "", instanceId: "", provision: undefined };
}

export const CommandToInstance = {
  encode(message: CommandToInstance, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.projectId !== "") {
      writer.uint32(10).string(message.projectId);
    }
    if (message.instanceId !== "") {
      writer.uint32(18).string(message.instanceId);
    }
    if (message.provision !== undefined) {
      Provision.encode(message.provision, writer.uint32(26).fork()).ldelim();
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

          message.projectId = reader.string();
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

          message.provision = Provision.decode(reader, reader.uint32());
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
      projectId: isSet(object.projectId) ? globalThis.String(object.projectId) : "",
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      provision: isSet(object.provision) ? Provision.fromJSON(object.provision) : undefined,
    };
  },

  toJSON(message: CommandToInstance): unknown {
    const obj: any = {};
    if (message.projectId !== "") {
      obj.projectId = message.projectId;
    }
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.provision !== undefined) {
      obj.provision = Provision.toJSON(message.provision);
    }
    return obj;
  },

  create(base?: DeepPartial<CommandToInstance>): CommandToInstance {
    return CommandToInstance.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<CommandToInstance>): CommandToInstance {
    const message = createBaseCommandToInstance();
    message.projectId = object.projectId ?? "";
    message.instanceId = object.instanceId ?? "";
    message.provision = (object.provision !== undefined && object.provision !== null)
      ? Provision.fromPartial(object.provision)
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
      requestType: FromInstance,
      requestStream: true,
      responseType: CommandToInstance,
      responseStream: true,
      options: {},
    },
  },
} as const;

export interface CacapcityServiceImplementation<CallContextExt = {}> {
  reportAsInstance(
    request: AsyncIterable<FromInstance>,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<CommandToInstance>>;
}

export interface CacapcityServiceClient<CallOptionsExt = {}> {
  reportAsInstance(
    request: AsyncIterable<DeepPartial<FromInstance>>,
    options?: CallOptions & CallOptionsExt,
  ): AsyncIterable<CommandToInstance>;
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
