import { ZodType } from "zod";
import { ZZEnv } from "./ZZEnv";

import { createHash } from "crypto";
import { getLogger } from "../utils/createWorkerLogger";
import { z } from "zod";
import { addDatapoint, ensureStreamRec } from "../db/knexConn";
import { v4 } from "uuid";
import { zodToJsonSchema } from "zod-to-json-schema";

const REDIS_CLIENT_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

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

// cursor based redis stream subscriber
import { Redis } from "ioredis";
import { Observable, Subscriber } from "rxjs";
import { createLazyNextValueGenerator } from "../realtime/pubsub";

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
      if (zzEnv.db) {
        ensureStreamRec({
          projectId: zzEnv.projectId,
          streamId: uniqueName,
          dbConn: zzEnv.db,
        });
      }
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
    this.logger = logger;
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
    let parsed: T;
    try {
      parsed = this.def.parse(message) as T;
    } catch (err) {
      console.error("errornous output: ", message);
      this.logger.error(
        `Data point error for stream ${
          this.uniqueName
        }: data provided is invalid: ${JSON.stringify(err)}`
      );
      throw err;
    }

    if (this.zzEnv.db) {
      const datapointId = messageIdOverride || v4();
      await addDatapoint({
        streamId: this.uniqueName,
        projectId: this.zzEnv.projectId,
        dbConn: this.zzEnv.db,
        jobInfo: jobInfo,
        data: parsed,
        datapointId,
      });
    }

    const { channelId, clients } = await getStreamClientsById({
      queueId: this.uniqueName,
      zzEnv: this.zzEnv,
    });

    try {
      // Publish the data to the stream
      const messageId = await clients.pub.xadd(
        channelId,
        "*",
        "data",
        customStringify(parsed)
      );

      return messageId;
    } catch (error) {
      console.error("Error publishing to stream:", error);
      throw error;
    }
  }

  public subFromNow() {
    return new ZZStreamSubscriber({
      stream: this,
      zzEnv: this.zzEnv,
      initialCursor: "$",
    });
  }

  public subFromBeginning() {
    return new ZZStreamSubscriber({
      stream: this,
      zzEnv: this.zzEnv,
      initialCursor: "0",
    });
  }
}

export class ZZStreamSubscriber<T> {
  private zzEnv: ZZEnv;
  private stream: ZZStream<T>;
  private cursor: string;
  private _valueObservable: Observable<T> | null = null;
  private isUnsubscribed: boolean = false;
  private _nextValue: (() => Promise<T>) | null = null;

  constructor({
    stream,
    zzEnv,
    initialCursor,
  }: {
    stream: ZZStream<T>;
    zzEnv: ZZEnv;
    initialCursor: "0" | "$";
  }) {
    this.stream = stream;
    this.zzEnv = zzEnv;
    this.cursor = initialCursor;
  }

  private initializeObservable() {
    this._valueObservable = new Observable((subscriber: Subscriber<T>) => {
      this.readStream(subscriber);
    });
  }

  private async readStream(subscriber: Subscriber<T>) {
    const { channelId, clients } = getStreamClientsById({
      queueId: this.stream.uniqueName,
      zzEnv: this.zzEnv,
    });

    try {
      while (!this.isUnsubscribed) {
        // XREAD with block and count parameters
        const stream = await clients.sub.xread(
          "BLOCK",
          1000, // Set a timeout for blocking, e.g., 1000 milliseconds
          "STREAMS",
          channelId,
          this.cursor
        );
        if (stream) {
          const messages = stream[0][1]; // Assuming single stream
          for (let message of messages) {
            this.cursor = message[0];
            const data: T = this.parseMessageData(message[1]);
            subscriber.next(data);
          }
        }
      }
    } catch (error) {
      subscriber.error(error);
    } finally {
      // Perform cleanup here if necessary
      clients.sub.disconnect();
      subscriber.complete();
      subscriber.unsubscribe();
    }
  }

  private parseMessageData(data: Array<any>): T {
    // Look for the 'data' key and its subsequent value in the flattened array
    const dataIdx = data.indexOf("data");
    if (dataIdx === -1 || dataIdx === data.length - 1) {
      console.error("data:", data);
      throw new Error("Data key not found in stream message or is malformed");
    }

    const jsonData = data[dataIdx + 1];

    // Parse the JSON data (assuming data is stored as a JSON string)
    try {
      const parsedData = customParse(jsonData);
      return parsedData as T;
    } catch (error) {
      throw new Error(`Error parsing data from stream: ${error}`);
    }
  }

  public get valueObservable(): Observable<T> {
    if (!this._valueObservable) {
      this.initializeObservable();
    }
    return this._valueObservable!;
  }

  public unsubscribe = () => {
    this.isUnsubscribed = true;
    // Perform any additional cleanup or resource release here if necessary
  };

  public nextValue = () => {
    if (!this._nextValue) {
      const { nextValue } = createLazyNextValueGenerator(this.valueObservable);
      this._nextValue = () => nextValue();
    }
    return this._nextValue();
  };
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

function getStreamClientsById({
  queueId,
  zzEnv,
}: {
  queueId: string;
  zzEnv: ZZEnv;
}) {
  // const channelId = `zzstream:${zzEnv.projectId}/${queueId!}`;
  const channelId = `${queueId!}`;
  if (!REDIS_CLIENT_BY_ID[channelId]) {
    const sub = new Redis(zzEnv.redisConfig);
    const pub = new Redis(zzEnv.redisConfig);
    REDIS_CLIENT_BY_ID[channelId] = { sub, pub };
  }
  return { channelId, clients: REDIS_CLIENT_BY_ID[channelId] };
}
