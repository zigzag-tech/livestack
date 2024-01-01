import { ZodType } from "zod";
import Redis from "ioredis";
import { ProjectConfig } from "../config-factory/config-defs";
import { Observable } from "rxjs";
import { ZZEnv } from "./ZZEnv";

import { createHash } from "crypto";
const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

export type InferStreamDef<T> = T extends ZZStream<infer P> ? P : never;

export class ZZStream<T> {
  public readonly def: ZodType<T>;
  public readonly uniqueName: string;
  private static _projectConfig: ProjectConfig;
  private static _zzEnv: ZZEnv;
  public readonly hash: string;

  public static get zzEnv() {
    return ZZStream._zzEnv;
  }

  public static set zzEnv(env: ZZEnv) {
    ZZStream._zzEnv = env;
  }

  private _valueObservable: Observable<T> | null = null;

  static globalRegistry: { [key: string]: ZZStream<any> } = {};

  public static setProjectConfig(projectConfig: ProjectConfig) {
    ZZStream._projectConfig = projectConfig;
  }

  public static get<T>({
    uniqueName,
    def,
  }: {
    uniqueName: string;
    def: ZodType<T>;
  }): ZZStream<T> {
    if (ZZStream.globalRegistry[uniqueName]) {
      const existing = ZZStream.globalRegistry[uniqueName];
      // check if types match
      if (existing.def !== def) {
        throw new Error(
          `ZZStream ${uniqueName} already exists with different type`
        );
      }
      return existing;
    } else {
      const stream = new ZZStream({ uniqueName, def });
      ZZStream.globalRegistry[uniqueName] = stream;
      return stream;
    }
  }

  protected constructor({
    uniqueName,
    def,
  }: {
    uniqueName: string;
    def: ZodType<T>;
  }) {
    this.def = def;
    this.uniqueName = uniqueName;
    // use sha hash on def to get the unique hash
    const str = JSON.stringify(this.def);
    this.hash = createHash("sha256").update(str).digest("hex");
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

  public async emitValue(o: T) {
    this.pubToJob(o);
  }

  get valueObsrvable() {
    return this.ensureValueObservable();
  }

  public async pubToJob<T>(m: T) {
    return await this._pub(m);
  }

  private getPubSubClientsById({ queueId }: { queueId: string }) {
    const id = `msgq:${ZZStream._projectConfig.projectId}--${queueId!}`;
    if (!PUBSUB_BY_ID[id]) {
      const sub = new Redis(ZZStream._zzEnv.redisConfig);
      sub.subscribe(id, (err, count) => {
        if (err) {
          console.error("Failed to subscribe: %s", err.message);
        } else {
          // console.info(
          //   `getPubSubClientsById: subscribed successfully! This client is currently subscribed to ${count} channels.`
          // );
        }
      });
      const pub = new Redis(ZZStream._zzEnv.redisConfig);
      PUBSUB_BY_ID[id] = { sub, pub };
    }
    return { channelId: id, clients: PUBSUB_BY_ID[id] };
  }

  private async _pub<T>(msg: T) {
    const { channelId, clients } = await this.getPubSubClientsById({
      queueId: this.uniqueName,
    });

    // console.log("pubbing", channelId);

    const addedMsg = await clients.pub.publish(channelId, customStringify(msg));
    return addedMsg;
  }

  public sub({ processor }: { processor: (message: T) => void }) {
    const { clients, channelId } = this.getPubSubClientsById({
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
