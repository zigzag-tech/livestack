import { ZodType } from "zod";
import { ZZEnv } from "./ZZEnv";
import { saveLargeFilesToStorage } from "../storage/cloudStorage";
// import { createHash } from "crypto";
import { getLogger } from "../utils/createWorkerLogger";
import { v4 } from "uuid";
import { zodToJsonSchema } from "zod-to-json-schema";
import {
  identifyLargeFilesToSave,
  identifyLargeFilesToRestore,
  restoreLargeValues,
} from "../files/file-ops";
import path from "path";

export type InferStreamDef<T> = T extends DataStream<infer P> ? P : never;

export namespace DataStream {
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
import { Observable, Subscriber } from "rxjs";
import { createLazyNextValueGenerator } from "../realtime/pubsub";
import { dbClient, streamClient } from "@livestack/vault-client";
import { SubType } from "@livestack/vault-interface/src/generated/stream";

export class DataStream<T extends object> {
  public readonly def: ZodType<T> | null;
  public readonly uniqueName: string;
  // public readonly hash: string;
  private _zzEnv: ZZEnv | null = null;
  baseWorkingRelativePath: string;

  public get zzEnv() {
    const resolved = this._zzEnv || ZZEnv.global();
    if (!resolved) {
      throw new Error("zzEnv not set.");
    }
    return resolved;
  }
  private logger: ReturnType<typeof getLogger>;

  protected static globalRegistry: { [key: string]: DataStream<any> } = {};

  public static async getOrCreate<T extends object>({
    uniqueName,
    def,
    zzEnv,
    logger,
  }: {
    uniqueName: string;
    def?: ZodType<T> | null;
    zzEnv?: ZZEnv | null;
    logger?: ReturnType<typeof getLogger>;
  }): Promise<DataStream<T>> {
    if (!zzEnv) {
      zzEnv = ZZEnv.global();
    }
    if (DataStream.globalRegistry[uniqueName]) {
      const existing = DataStream.globalRegistry[uniqueName];
      // check if types match
      // TODO: use a more robust way to check if types match
      // TODO: to bring back this check
      // if (def) {
      //   if (existing.hash !== hashDef(def)) {
      //     throw new Error(
      //       `DataStream ${uniqueName} already exists with different type, and the new type provided is not compatible with the existing type.`
      //     );
      //   }
      // }
      return existing as DataStream<T>;
    } else {
      if (!logger) {
        throw new Error(
          "def and logger must be provided if stream does not exist."
        );
      }
      const stream = new DataStream({
        uniqueName,
        def: def || null,
        zzEnv,
        logger,
      });
      // async
      if (zzEnv) {
        await dbClient.ensureStreamRec({
          project_id: zzEnv.projectId,
          stream_id: uniqueName,
        });
      }
      DataStream.globalRegistry[uniqueName] = stream;
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
    def: ZodType<T> | null;
    zzEnv?: ZZEnv | null;
    logger: ReturnType<typeof getLogger>;
  }) {
    this.def = def;
    this.uniqueName = uniqueName;
    this._zzEnv = zzEnv || null;
    // this.hash = hashDef(this.def);
    this.logger = logger;
    // console.debug(
    //   "DataStream created",
    //   this.uniqueName,
    //   JSON.stringify(zodToJsonSchema(this.def), null, 2)
    // );
    this.baseWorkingRelativePath = path.join(
      this.zzEnv.projectId,
      this.uniqueName
    );
  }

  public lastValue = async () => {
    const { null_response, datapoint } = await streamClient.lastValue({
      projectId: this.zzEnv.projectId,
      uniqueName: this.uniqueName,
    });
    if (null_response) {
      return null;
    } else if (datapoint) {
      const data = customParse(datapoint.dataStr);
      return {
        ...data,
        timestamp: datapoint.timestamp,
        messageId: datapoint.messageId,
      };
    } else {
      throw new Error("Unexpected response from lastValue");
    }
  };

  public async pub({
    message,
    jobInfo,
  }: {
    message: T;
    jobInfo?: {
      jobId: string;
      outputTag: string;
    };
  }) {
    let parsed: T;
    if (!this.def) {
      parsed = message;
    } else {
      try {
        parsed = this.def.parse(message) as T;
      } catch (err) {
        this.logger.error("Data validation error" + JSON.stringify(err));
        console.log(this.uniqueName, " errornous output: ", message);
        this.logger.error(
          "Expected type: " + JSON.stringify(zodToJsonSchema(this.def), null, 2)
        );
        this.logger.error(
          `Data point error for stream ${
            this.uniqueName
          }: data provided is invalid: ${JSON.stringify(err)}`
        );
        throw err;
      }
    }

    let { largeFilesToSave, newObj } = identifyLargeFilesToSave(parsed);

    if (this.zzEnv.storageProvider) {
      const fullPathLargeFilesToSave = largeFilesToSave.map((x) => ({
        ...x,
        path: path.join(this.baseWorkingRelativePath, x.path),
      }));

      if (fullPathLargeFilesToSave.length > 0) {
        // this.logger.info(
        //   `Saving large files to storage: ${fullPathLargeFilesToSave
        //     .map((x) => x.path)
        //     .join(", ")}`
        // );
        await saveLargeFilesToStorage(
          fullPathLargeFilesToSave,
          this.zzEnv.storageProvider
        );
        parsed = newObj;
      }
    } else {
      if (largeFilesToSave.length > 0) {
        throw new Error(
          "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
        );
      }
    }

    const datapointId = v4();

    try {
      // Publish the data to the stream
      const [_, { messageId }] = await Promise.all([
        dbClient.addDatapoint({
          streamId: this.uniqueName,
          projectId: this.zzEnv.projectId,
          jobInfo: jobInfo,
          dataStr: JSON.stringify(parsed),
          datapointId,
        }),
        streamClient.pub({
          projectId: this.zzEnv.projectId,
          uniqueName: this.uniqueName,
          dataStr: customStringify(parsed),
        }),
      ]);

      // console.debug("DataStream pub", this.uniqueName, parsed);

      return messageId;
    } catch (error) {
      console.error("Error publishing to stream:", error);
      throw error;
    }
  }

  public subFromNow() {
    return new DataStreamSubscriber({
      stream: this,
      zzEnv: this.zzEnv,
      initialCursor: "$",
      subType: SubType.fromNow,
    });
  }

  public subFromBeginning() {
    return new DataStreamSubscriber({
      stream: this,
      zzEnv: this.zzEnv,
      initialCursor: "0",
      subType: SubType.fromStart,
    });
  }

  public static define<T extends object>(
    ...p: ConstructorParameters<typeof DataStreamDef<T>>
  ) {
    return new DataStreamDef<T>(...p);
  }
}

export class DataStreamDef<T> {
  public readonly streamDefName: string;
  public readonly def?: ZodType<T>;

  constructor(streamDefName: string, def?: ZodType<T>) {
    this.streamDefName = streamDefName;
    this.def = def;
  }
}

export type WithTimestamp<T extends object> = T & {
  timestamp: number;
  messageId: string;
};

export class DataStreamSubscriber<T extends object> {
  private zzEnv: ZZEnv;
  private stream: DataStream<T>;
  private cursor: `${string}-${string}` | "$" | "0";
  private _valueObservable: Observable<WithTimestamp<T>> | null = null;
  private isUnsubscribed: boolean = false;
  private _nextValue: (() => Promise<WithTimestamp<T>>) | null = null;
  private subType: SubType;

  constructor({
    stream,
    zzEnv,
    initialCursor,
    subType,
  }: {
    stream: DataStream<T>;
    zzEnv: ZZEnv;
    initialCursor: "0" | "$";
    subType: SubType;
  }) {
    this.stream = stream;
    this.zzEnv = zzEnv;
    this.cursor = initialCursor;
    this.subType = subType;
  }

  private initializeObservable() {
    this._valueObservable = new Observable(
      (subscriber: Subscriber<WithTimestamp<T>>) => {
        this.readStream(subscriber);
      }
    );
  }

  private async readStream(subscriber: Subscriber<WithTimestamp<T>>) {
    try {
      const iter = streamClient.sub({
        projectId: this.zzEnv.projectId,
        uniqueName: this.stream.uniqueName,
        subType: this.subType,
      });

      for await (const message of iter) {
        const data: T = customParse(message.dataStr);
        let restored = data;
        const { largeFilesToRestore, newObj } =
          identifyLargeFilesToRestore(data);

        if (largeFilesToRestore.length > 0) {
          if (!this.zzEnv.storageProvider) {
            throw new Error(
              "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
            );
          } else {
            restored = (await restoreLargeValues({
              obj_: newObj,
              largeFilesToRestore,
              basePath: this.stream.baseWorkingRelativePath,
              fetcher: this.zzEnv.storageProvider.fetchFromStorage,
            })) as T;
          }
        }

        subscriber.next({
          ...restored,
          timestamp: message.timestamp,
          messageId: message.messageId,
        });
      }
    } catch (error) {
      subscriber.error(error);
    } finally {
      // Perform cleanup here if necessary
      subscriber.complete();
      subscriber.unsubscribe();
    }
  }

  public get valueObservable(): Observable<WithTimestamp<T>> {
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

  async *[Symbol.asyncIterator]() {
    while (true) {
      const input = await this.nextValue();

      // Assuming nextInput returns null or a similar value to indicate completion
      if (!input) {
        break;
      }
      yield input;
    }
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

// export function hashDef(def: ZodType<unknown>) {
//   const str = JSON.stringify(zodToJsonSchema(def));
//   return createHash("sha256").update(str).digest("hex");
// }
