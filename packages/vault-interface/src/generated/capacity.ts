/* eslint-disable */
import type { CallContext, CallOptions } from "nice-grpc-common";
import _m0 from "protobufjs/minimal";

export const protobufPackage = "livestack";

export interface ReportCapacityAvailability {
  instanceId: string;
  specName: string;
  maxCapacity: number;
}

export interface FromInstance {
  instanceId: string;
  reportCapacityAvailability?: ReportCapacityAvailability | undefined;
}

export interface Provision {
  specName: string;
  numberOfWorkersNeeded: number;
}

export interface CommandToInstance {
  instanceId: string;
  provision?: Provision | undefined;
}

function createBaseReportCapacityAvailability(): ReportCapacityAvailability {
  return { instanceId: "", specName: "", maxCapacity: 0 };
}

export const ReportCapacityAvailability = {
  encode(message: ReportCapacityAvailability, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.instanceId !== "") {
      writer.uint32(10).string(message.instanceId);
    }
    if (message.specName !== "") {
      writer.uint32(18).string(message.specName);
    }
    if (message.maxCapacity !== 0) {
      writer.uint32(24).int32(message.maxCapacity);
    }
    return writer;
  },

  decode(input: _m0.Reader | Uint8Array, length?: number): ReportCapacityAvailability {
    const reader = input instanceof _m0.Reader ? input : _m0.Reader.create(input);
    let end = length === undefined ? reader.len : reader.pos + length;
    const message = createBaseReportCapacityAvailability();
    while (reader.pos < end) {
      const tag = reader.uint32();
      switch (tag >>> 3) {
        case 1:
          if (tag !== 10) {
            break;
          }

          message.instanceId = reader.string();
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

  fromJSON(object: any): ReportCapacityAvailability {
    return {
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      maxCapacity: isSet(object.maxCapacity) ? globalThis.Number(object.maxCapacity) : 0,
    };
  },

  toJSON(message: ReportCapacityAvailability): unknown {
    const obj: any = {};
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.specName !== "") {
      obj.specName = message.specName;
    }
    if (message.maxCapacity !== 0) {
      obj.maxCapacity = Math.round(message.maxCapacity);
    }
    return obj;
  },

  create(base?: DeepPartial<ReportCapacityAvailability>): ReportCapacityAvailability {
    return ReportCapacityAvailability.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<ReportCapacityAvailability>): ReportCapacityAvailability {
    const message = createBaseReportCapacityAvailability();
    message.instanceId = object.instanceId ?? "";
    message.specName = object.specName ?? "";
    message.maxCapacity = object.maxCapacity ?? 0;
    return message;
  },
};

function createBaseFromInstance(): FromInstance {
  return { instanceId: "", reportCapacityAvailability: undefined };
}

export const FromInstance = {
  encode(message: FromInstance, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.instanceId !== "") {
      writer.uint32(10).string(message.instanceId);
    }
    if (message.reportCapacityAvailability !== undefined) {
      ReportCapacityAvailability.encode(message.reportCapacityAvailability, writer.uint32(18).fork()).ldelim();
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

          message.instanceId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
            break;
          }

          message.reportCapacityAvailability = ReportCapacityAvailability.decode(reader, reader.uint32());
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
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      reportCapacityAvailability: isSet(object.reportCapacityAvailability)
        ? ReportCapacityAvailability.fromJSON(object.reportCapacityAvailability)
        : undefined,
    };
  },

  toJSON(message: FromInstance): unknown {
    const obj: any = {};
    if (message.instanceId !== "") {
      obj.instanceId = message.instanceId;
    }
    if (message.reportCapacityAvailability !== undefined) {
      obj.reportCapacityAvailability = ReportCapacityAvailability.toJSON(message.reportCapacityAvailability);
    }
    return obj;
  },

  create(base?: DeepPartial<FromInstance>): FromInstance {
    return FromInstance.fromPartial(base ?? {});
  },
  fromPartial(object: DeepPartial<FromInstance>): FromInstance {
    const message = createBaseFromInstance();
    message.instanceId = object.instanceId ?? "";
    message.reportCapacityAvailability =
      (object.reportCapacityAvailability !== undefined && object.reportCapacityAvailability !== null)
        ? ReportCapacityAvailability.fromPartial(object.reportCapacityAvailability)
        : undefined;
    return message;
  },
};

function createBaseProvision(): Provision {
  return { specName: "", numberOfWorkersNeeded: 0 };
}

export const Provision = {
  encode(message: Provision, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.specName !== "") {
      writer.uint32(10).string(message.specName);
    }
    if (message.numberOfWorkersNeeded !== 0) {
      writer.uint32(16).int32(message.numberOfWorkersNeeded);
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

  fromJSON(object: any): Provision {
    return {
      specName: isSet(object.specName) ? globalThis.String(object.specName) : "",
      numberOfWorkersNeeded: isSet(object.numberOfWorkersNeeded) ? globalThis.Number(object.numberOfWorkersNeeded) : 0,
    };
  },

  toJSON(message: Provision): unknown {
    const obj: any = {};
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
    message.specName = object.specName ?? "";
    message.numberOfWorkersNeeded = object.numberOfWorkersNeeded ?? 0;
    return message;
  },
};

function createBaseCommandToInstance(): CommandToInstance {
  return { instanceId: "", provision: undefined };
}

export const CommandToInstance = {
  encode(message: CommandToInstance, writer: _m0.Writer = _m0.Writer.create()): _m0.Writer {
    if (message.instanceId !== "") {
      writer.uint32(10).string(message.instanceId);
    }
    if (message.provision !== undefined) {
      Provision.encode(message.provision, writer.uint32(18).fork()).ldelim();
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

          message.instanceId = reader.string();
          continue;
        case 2:
          if (tag !== 18) {
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
      instanceId: isSet(object.instanceId) ? globalThis.String(object.instanceId) : "",
      provision: isSet(object.provision) ? Provision.fromJSON(object.provision) : undefined,
    };
  },

  toJSON(message: CommandToInstance): unknown {
    const obj: any = {};
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
    reportInstance: {
      name: "ReportInstance",
      requestType: FromInstance,
      requestStream: true,
      responseType: CommandToInstance,
      responseStream: true,
      options: {},
    },
  },
} as const;

export interface CacapcityServiceImplementation<CallContextExt = {}> {
  reportInstance(
    request: AsyncIterable<FromInstance>,
    context: CallContext & CallContextExt,
  ): ServerStreamingMethodResult<DeepPartial<CommandToInstance>>;
}

export interface CacapcityServiceClient<CallOptionsExt = {}> {
  reportInstance(
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
