import { ZodType } from "zod";
import Redis from "ioredis";
import { Observable } from "rxjs";
import { ZZEnv } from "./ZZEnv";

import { createHash } from "crypto";
import { createLazyNextValueGenerator } from "../realtime/pubsub";
import { getLogger } from "../utils/createWorkerLogger";
import { z } from "zod";
import { addDatapoint, ensureStreamRec } from "../db/knexConn";
import { v4 } from "uuid";
import { zodToJsonSchema } from "zod-to-json-schema";

const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

export type InferStreamDef<T> = T extends ZZStream<infer P> ? P : never;

export namespace ZZStream {
  export type single<ZT> = ZT extends ZodType<infer T>
    ? {
        default: T;
      }
    : never;

  export type multi<ZTMap> = ZTMap extends {
    [K in keyof ZTMap]: ZodType<ZTMap[K]>;
  }
    ? {
        [K in keyof ZTMap]: ZTMap[K];
      }
    : never;
}

export class ZZStream<T> {
  public readonly def: ZodType<T>;
  public readonly uniqueName: string;
  public readonly hash: string;
  private zzEnv: ZZEnv;
  private logger: ReturnType<typeof getLogger>;

  public static single<T>(def: z.ZodType<T>) {
    return {
      default: def,
    };
  }

  nextValue: () => Promise<T>;

  private _valueObservable: Observable<T> | null = null;

  protected static globalRegistry: { [key: string]: ZZStream<unknown> } = {};

  public static async getOrCreate<T>({
    uniqueName,
    def,
    zzEnv,
    logger,
  }: {
    uniqueName: string;
    def?: ZodType<T>;
    zzEnv?: ZZEnv;
    logger?: ReturnType<typeof getLogger>;
  }): Promise<ZZStream<T>> {
    if (!zzEnv) {
      zzEnv = ZZEnv.global();
    }
    if (ZZStream.globalRegistry[uniqueName]) {
      const existing = ZZStream.globalRegistry[uniqueName];
      // check if types match
      // TODO: use a more robust way to check if types match
      if (def) {
        if (existing.hash !== hashDef(def)) {
          throw new Error(
            `ZZStream ${uniqueName} already exists with different type, and the new type provided is not compatible with the existing type.`
          );
        }
      }
      return existing as ZZStream<T>;
    } else {
      if (!def || !logger) {
        throw new Error(
          "def and logger must be provided if stream does not exist"
        );
      }
      const stream = new ZZStream({ uniqueName, def, zzEnv, logger });
      // async
      ensureStreamRec({
        projectId: zzEnv.projectId,
        streamId: uniqueName,
        dbConn: zzEnv.db,
      });
      ZZStream.globalRegistry[uniqueName] = stream;
      return stream;
    }
  }

  protected constructor({
    uniqueName,
    def,
    zzEnv,
    logger,
  }: {
    uniqueName: string;
    def: ZodType<T>;
    zzEnv: ZZEnv;
    logger: ReturnType<typeof getLogger>;
  }) {
    this.def = def;
    this.uniqueName = uniqueName;
    this.zzEnv = zzEnv;
    this.hash = hashDef(this.def);
    const { nextValue } = createLazyNextValueGenerator(this.valueObsrvable);
    this.nextValue = nextValue;
    this.logger = logger;
  }

  private ensureValueObservable() {
    if (!this._valueObservable) {
      this._valueObservable = new Observable<T>((subscriber) => {
        const { unsub } = this.sub({
          processor: async (v) => {
            subscriber.next(v);
          },
        });
        return {
          unsubscribe: () => {
            unsub();
          },
        };
      });
    }

    return this._valueObservable;
  }

  public async pub({
    message,
    jobInfo,
    messageIdOverride,
  }: {
    message: T;
    jobInfo?: {
      jobId: string;
      jobOutputKey: string;
    };
    messageIdOverride?: string;
  }) {
    const datapointId = messageIdOverride || v4();
    await addDatapoint({
      streamId: this.uniqueName,
      projectId: this.zzEnv.projectId,
      dbConn: this.zzEnv.db,
      jobInfo: jobInfo,
      data: message,
      datapointId,
    });

    const { channelId, clients } = await this.getStreamClientsById({
      queueId: this.uniqueName,
    });

    // console.log("pubbing", channelId);

    try {
      const parsed = this.def.parse(message) as T;
      const addedMsg = await clients.pub.publish(
        channelId,
        customStringify(parsed)
      );
      return addedMsg;
    } catch (err) {
      console.error("errornous output: ", message);
      this.logger.error(
        `Data point error for stream ${
          this.uniqueName
        }: data provided is invalid: ${JSON.stringify(err)}`
      );
      throw err;
    }
  }

  get valueObsrvable() {
    return this.ensureValueObservable();
  }

  private getStreamClientsById({ queueId }: { queueId: string }) {
    const id = `zzmsgq:${this.zzEnv.projectId}--${queueId!}`;
    if (!PUBSUB_BY_ID[id]) {
      const sub = new Redis(this.zzEnv.redisConfig);
      sub.subscribe(id, (err, count) => {
        if (err) {
          console.error("Failed to subscribe: %s", err.message);
        } else {
          // console.info(
          //   `getPubSubClientsById: subscribed successfully! This client is currently subscribed to ${count} channels.`
          // );
        }
      });
      const pub = new Redis(this.zzEnv.redisConfig);
      PUBSUB_BY_ID[id] = { sub, pub };
    }
    return { channelId: id, clients: PUBSUB_BY_ID[id] };
  }

  public sub({ processor }: { processor: (message: T) => void }) {
    const { clients, channelId } = this.getStreamClientsById({
      queueId: this.uniqueName,
    });

    // console.log("sub to", channelId);

    clients.sub.on("message", async (channel, message) => {
      const msg = customParse(message);
      await processor(msg);
    });

    const unsub = async () => {
      await clients.sub.unsubscribe();
    };

    return {
      unsub,
    };
  }
}

// TODO: make internal

function customStringify(obj: any): string {
  function replacer(key: string, value: any): any {
    if (value instanceof Buffer) {
      return { type: "Buffer", data: value.toString("base64") };
    }
    return value;
  }
  return JSON.stringify(obj, replacer);
}

function customParse(json: string): any {
  function reviver(key: string, value: any): any {
    if (value && value.type === "Buffer") {
      return Buffer.from(value.data, "base64");
    } else {
      return value;
    }
  }
  return JSON.parse(json, reviver);
}

export function hashDef(def: ZodType<unknown>) {
  const str = JSON.stringify(zodToJsonSchema(def));
  return createHash("sha256").update(str).digest("hex");
}
