import { ZZWorkerDefParams, ZZWorkerDef } from "./ZZWorker";
import { InferStreamSetType } from "@livestack/shared/StreamDefSet";
import { JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { GenericRecordType, QueueName } from "../orchestrations/workerCommon";
import Redis from "ioredis";
import _ from "lodash";
import {
  ensureJobAndInitStatusRec,
  ensureJobStreamConnectorRec,
  ensureStreamRec,
  getJobDatapoints,
  getJobRec,
  getJobStreamConnectorRecs,
} from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";

import { ZodType, z } from "zod";
import { ZZEnv } from "./ZZEnv";
import {
  WrapTerminateFalse,
  WrapTerminatorAndDataId,
  wrapStreamSubscriberWithTermination,
  wrapTerminatorAndDataId,
} from "../utils/io";
import { WithTimestamp, ZZStream, ZZStreamSubscriber } from "./ZZStream";
import pLimit from "p-limit";
import { ZZJobSpecBase } from "@livestack/shared/ZZJobSpecBase";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof getCachedQueueByName>
>();

// export type CheckTMap<T> = T extends Record<string, infer V> ? T : never;

export type CheckSpec<SP> = SP extends ZZJobSpec<
  infer P,
  infer I,
  infer O,
  infer IMap,
  infer OMap
>
  ? ZZJobSpec<P, I, O, IMap, OMap>
  : never;

export type InferOutputType<
  Spec,
  K,
  OMap = InferStreamSetType<CheckSpec<Spec>["outputDefSet"]>
> = OMap[K extends keyof OMap ? K : never] | null;

export type InferInputType<
  Spec,
  K,
  IMap = InferStreamSetType<CheckSpec<Spec>["inputDefSet"]>
> = IMap[K extends keyof IMap ? K : never] | null;

export type InferTMap<T> = T extends z.ZodType
  ? { default: z.infer<T> }
  : T extends Record<string, ZodType>
  ? {
      [K in keyof T]: z.infer<T[K]>;
    }
  : never;

export class ZZJobSpec<
  P = {},
  I = any,
  O = any,
  IMap = InferTMap<I>,
  OMap = InferTMap<O>
> extends ZZJobSpecBase<P, IMap, OMap> {
  private readonly _zzEnv: ZZEnv | null = null;
  protected static _registryBySpecName: Record<
    string,
    ZZJobSpec<any, any, any, any, any>
  > = {};

  protected logger: ReturnType<typeof getLogger>;

  public get zzEnv() {
    const resolved = this._zzEnv || ZZEnv.global();
    if (!resolved) {
      throw new Error(
        `ZZEnv is not configured in ZZJobSpec ${this.name}. \nPlease either pass it when constructing ZZJobSpec or set it globally using ZZEnv.setGlobal().`
      );
    }
    return resolved;
  }

  readonly jobOptions: z.ZodType<P>;

  constructor({
    zzEnv,
    name,
    jobOptions,
    output,
    input,
  }: {
    name: string;
    jobOptions?: z.ZodType<P>;
    input?: I;
    output?: O;
    zzEnv?: ZZEnv;
    concurrency?: number;
  }) {
    super({
      name,
      input: wrapIfSingle(input),
      output: wrapIfSingle(output),
    });

    this.jobOptions = jobOptions || (z.object({}) as unknown as z.ZodType<P>);
    this._zzEnv = zzEnv || null;
    this.logger = getLogger(`spec:${this.name}`);
    if (!output) {
      this.logger.warn(`No output defined for job spec ${this.name}.`);
    }
    ZZJobSpec._registryBySpecName[this.name] = this;
  }

  public static lookupByName(specName: string) {
    if (!ZZJobSpec._registryBySpecName[specName]) {
      throw new Error(`JobSpec ${specName} not defined on this machine.`);
    }
    return ZZJobSpec._registryBySpecName[specName];
  }

  public derive<NewP, NewIMap, NewOMap>(
    newP: Partial<
      ConstructorParameters<typeof ZZJobSpec<NewP, NewIMap, NewOMap>>[0]
    > & {
      name: string;
    }
  ) {
    if (newP.name === this.name) {
      throw new Error(
        `Derived job spec must have a different name from the original job spec ${this.name}.`
      );
    }
    return new ZZJobSpec<P & NewP, IMap & NewIMap, OMap & NewOMap>({
      ...newP,
      name: newP.name,
    } as ConstructorParameters<typeof ZZJobSpec<P & NewP, IMap & NewIMap, OMap & NewOMap>>[0]);
  }

  private get _rawQueue() {
    return getCachedQueueByName<P, void>({
      queueNameOnly: this.name,
      queueOptions: {
        connection: this.zzEnv.redisConfig,
      },
      projectId: this.zzEnv.projectId,
    });
  }

  // public async getJob(jobId: string) {
  //   const j = await this._rawQueue.getJob(jobId);
  //   return j || null;
  // }

  public async getJobRec(jobId: string) {
    if (!this.zzEnv.db) {
      throw new Error(
        "Database is not configured in ZZEnv, therefore can not get job record."
      );
    }
    return await getJobRec({
      specName: this.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
    });
  }

  public async getJobStatus(jobId: string) {
    const job = await this.getJobRec(jobId);
    return job?.status || null;
  }

  public async getJobData<
    T extends "in" | "out",
    K extends T extends "in" ? keyof IMap : keyof OMap,
    U = T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
  >({
    jobId,
    ioType = "out" as T,
    order,
    limit,
    key,
  }: {
    jobId: string;
    ioType?: T;
    order?: "asc" | "desc";
    limit?: number;
    key?: K;
  }) {
    if (!this.zzEnv.db) {
      throw new Error(
        "Database is not configured in ZZEnv, therefore can not get job data."
      );
    }
    if (!key) {
      if (ioType === "in") {
        key = this.getSingleInputTag() as typeof key;
      } else {
        key = this.getSingleOutputTag() as typeof key;
      }
    }
    const recs = await getJobDatapoints<
      WithTimestamp<WrapTerminatorAndDataId<U>>
    >({
      specName: this.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
      key: String(key),
    });
    const points = recs
      .map((r) => r.data)
      .filter((r) => r.terminate === false)
      .map((r) => ({
        data: (r as WithTimestamp<WrapTerminateFalse<U>>).data as U,
        timestamp: r.timestamp,
      }));
    return points;
  }

  public async enqueueJobAndWaitOnSingleResults<K extends keyof OMap>({
    jobId: jobId,
    jobOptions: jobOptions,
    outputTag,
  }: // queueEventsOptions,
  {
    jobId?: string;
    jobOptions?: P;
    outputTag?: K;
  }): Promise<{ data: OMap[K]; timestamp: number } | null> {
    if (!jobId) {
      jobId = `${this.name}-${v4()}`;
    }

    if (!outputTag) {
      outputTag = this.getSingleOutputTag() as K;
    }

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(jobOptions)}.`
    );

    const { output, input } = await this.enqueueJob({
      jobId,
      jobOptions,
    });

    const r = await output.byTag(outputTag).nextValue();
    input.keys.forEach((k) => {
      input.terminate(k);
    });
    return r;
  }

  async feedJobInput<K extends keyof IMap>({
    jobId,
    data,
    tag,
  }: {
    jobId: string;
    data: IMap[K];
    tag: K;
  }) {
    await this._getStreamAndSendDataToPLimited({
      jobId,
      tag: tag,
      type: "in",
      data: {
        data,
        terminate: false,
      },
    });
  }

  private _sendFnsByJobIdAndKey: {
    [jobId: string]: Partial<{
      in: Partial<{
        [key in keyof IMap]: ReturnType<typeof pLimit>;
      }>;
      out: Partial<{
        [key in keyof OMap]: ReturnType<typeof pLimit>;
      }>;
    }>;
  } = {};

  public async _getStreamAndSendDataToPLimited<T extends "in" | "out">({
    jobId,
    tag,
    type,
    data: d,
  }: {
    jobId: string;
    tag: T extends "in" ? keyof IMap : keyof OMap;
    type: T;
    data: WrapTerminatorAndDataId<
      T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
    >;
  }) {
    if (!this._sendFnsByJobIdAndKey[jobId]) {
      this._sendFnsByJobIdAndKey[jobId] = {};
    }
    if (!this._sendFnsByJobIdAndKey[jobId][type]) {
      this._sendFnsByJobIdAndKey[jobId][type] = {};
    }
    const dict = this._sendFnsByJobIdAndKey[jobId][type]! as Partial<{
      [key in T extends "in" ? keyof IMap : keyof OMap]: ReturnType<
        typeof pLimit
      >;
    }>;

    if (!dict[tag]) {
      dict[tag] = pLimit(1);
    }

    const limit = dict[tag]!;

    await limit(async () => {
      // console.debug("stream_data", JSON.stringify(d, longStringTruncator));

      const redis = new Redis(this.zzEnv.redisConfig);
      if (!d.terminate) {
        // const lastV = await stream.lastValue();
        // mark job terminated in redis key value store
        const isTerminated =
          (await redis.get(`terminated__${jobId}/${type}/${String(tag)}`)) ===
          "true";
        await redis.disconnect();
        if (isTerminated) {
          this.logger.error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream! jobId: ${jobId}, tag: ${String(tag)}`
          );
          throw new Error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream!`
          );
        }
      } else {
        await redis.set(
          `terminated__${jobId}/${type}/${String(tag)}`,
          "true",
          "EX",
          600
        );
      }
      await redis.disconnect();

      const stream = await this.getJobStream({
        jobId,
        tag: tag,
        type,
      });
      await stream.pub({
        message: d,
        ...(type === "out"
          ? {
              jobInfo: {
                jobId,
                jobOutputKey: String(tag),
              },
            }
          : {}),
      });
    });
  }

  async terminateJobInput({ jobId, tag }: { jobId: string; tag?: keyof IMap }) {
    await this._getStreamAndSendDataToPLimited({
      jobId,
      tag: tag || (this.getSingleInputTag() as keyof IMap),
      type: "in",
      data: {
        terminate: true,
      },
    });
  }

  public async waitForJobReadyForInputs({
    jobId,
    tag,
  }: {
    jobId: string;
    tag?: keyof IMap;
  }) {
    let isReady = await this.getJobReadyForInputsInRedis({
      jobId,
      key: tag ? tag : (this.getSingleInputTag() as keyof IMap),
    });
    while (!isReady) {
      await sleep(100);
      isReady = await this.getJobReadyForInputsInRedis({
        jobId,
        key: tag ? tag : (this.getSingleInputTag() as keyof IMap),
      });
      // console.log("isReady", isReady);
    }

    return {
      feedJobInput: <K extends keyof IMap>({
        data,
        key,
      }: {
        data: IMap[K];
        key?: K;
      }) => {
        const tag = key || (this.getSingleInputTag() as keyof IMap);
        this.feedJobInput({ jobId, data, tag: tag as K });
      },
      terminateJobInput: <K extends keyof IMap>(p?: { key?: K }) => {
        const tag = p?.key || (this.getSingleInputTag() as keyof IMap);
        this.terminateJobInput({
          jobId,
          tag: tag as K,
        });
      },
    };
  }

  getJobReadyForInputsInRedis = async <K extends keyof IMap>({
    jobId,
    key,
  }: {
    jobId: string;
    key: K;
  }) => {
    try {
      const redis = new Redis(this.zzEnv.redisConfig);
      const isReady = await redis.get(`ready_status__${jobId}/${String(key)}`);
      // console.debug("getJobReadyForInputsInRedis", jobId, key, ":", isReady);
      return isReady === "true";
    } catch (error) {
      console.error("Error getJobReadyForInputsInRedis:", error);
    }
  };

  private streamIdOverridesByTagByTypeByJobId: {
    [jobId: string]: {
      in: Partial<Record<keyof IMap, string>> | null;
      out: Partial<Record<keyof OMap, string>> | null;
    };
  } = {};

  private async getStreamIdOverride({
    jobId,
    type,
    key,
  }: {
    jobId: string;
    type: "in" | "out";
    key: keyof IMap | keyof OMap;
  }) {
    const overridesById = this.streamIdOverridesByTagByTypeByJobId[jobId];
    if (!overridesById) {
      this.streamIdOverridesByTagByTypeByJobId[jobId] = {
        in: null,
        out: null,
      };
    }

    // check if overridesByType is already cached
    // if not, fetch from db
    const overridesByType =
      this.streamIdOverridesByTagByTypeByJobId[jobId][type];
    if (!overridesByType && this.zzEnv.db) {
      const connectors = await getJobStreamConnectorRecs({
        projectId: this.zzEnv.projectId,
        dbConn: this.zzEnv.db,
        jobId,
        connectorType: type,
      });
      const overrides = _.fromPairs(
        connectors.map((c) => [c.key, c.stream_id])
      );
      this.streamIdOverridesByTagByTypeByJobId[jobId][type] =
        overrides as Partial<Record<keyof IMap | keyof OMap, string>>;
    }

    return (
      ((
        this.streamIdOverridesByTagByTypeByJobId[jobId][type]! as Record<
          any,
          string
        >
      )[
        key ||
          (type === "in"
            ? (this.getSingleInputTag() as keyof IMap)
            : (this.getSingleOutputTag() as keyof OMap))
      ] as string | undefined) || null
    );
  }

  public async getStreamIdForJob({
    jobId,
    type,
    p,
  }: {
    jobId: string;
    type: "in" | "out";
    p: {
      tag: keyof IMap | keyof OMap;
    };
  }) {
    let streamId: string;
    const streamIdOverride = await this.getStreamIdOverride({
      jobId,
      type,
      key: p.tag,
    });
    if (streamIdOverride) {
      streamId = streamIdOverride;
    } else {
      // const queueIdPrefix = ;
      // const queueId = `${type}/${String(p.key || "default")}`;
      streamId = deriveStreamId({
        groupId: `[${jobId}]`,
        from:
          type === "in"
            ? undefined
            : { specName: this.name, tag: String(p.tag) },
        to:
          type === "out"
            ? undefined
            : { specName: this.name, tag: String(p.tag) },
      });
    }
    return streamId;
  }

  public getInputJobStream = async <K extends keyof IMap>(p: {
    jobId: string;
    tag: K;
  }) => {
    return (await this.getJobStream({
      ...p,
      type: "in",
    })) as ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
  };

  public getOutputJobStream = async <K extends keyof OMap>(p: {
    jobId: string;
    tag: K;
  }) => {
    return (await this.getJobStream({
      ...p,
      type: "out",
    })) as ZZStream<WrapTerminatorAndDataId<OMap[K]>>;
  };

  private getJobStream = async <
    TT extends "in" | "out",
    K extends TT extends "in" ? keyof IMap : keyof OMap
  >(p: {
    jobId: string;
    type: TT;
    tag: K;
  }) => {
    const { jobId, type } = p;
    type T = TT extends "in" ? IMap[keyof IMap] : OMap[keyof OMap];
    let def: z.ZodType<WrapTerminatorAndDataId<T>>;
    if (type === "in") {
      if (!this.inputDefSet) {
        throw new Error(`No input defined for job spec ${this.name}`);
      }
      def = wrapTerminatorAndDataId(
        this.inputDefSet.getDef(
          (p.tag || this.getSingleInputTag()) as keyof IMap
        )
      ) as z.ZodType<WrapTerminatorAndDataId<T>>;
    } else if (type === "out") {
      def = wrapTerminatorAndDataId(
        this.outputDefSet.getDef(
          (p.tag || this.getSingleOutputTag()) as keyof OMap
        )
      ) as z.ZodType<WrapTerminatorAndDataId<T>>;
    } else {
      throw new Error(`Invalid type ${type}`);
    }
    const streamId = await this.getStreamIdForJob({
      jobId,
      type,
      p,
    });

    const stream = await ZZStream.getOrCreate<WrapTerminatorAndDataId<T>>({
      uniqueName: streamId,
      def,
      logger: this.logger,
      zzEnv: this.zzEnv,
    });
    return stream as ZZStream<WrapTerminatorAndDataId<T>>;
  };

  public createOutputCollector<K extends keyof OMap>({
    jobId,
    key,
    from = "beginning",
  }: {
    jobId: string;
    key?: K;
    from?: "beginning" | "now";
  }) {
    const subuscriberP = new Promise<
      ZZStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>
    >((resolve, reject) => {
      this.getOutputJobStream({
        jobId,
        tag: key || (this.getSingleOutputTag() as K),
      }).then((stream) => {
        let subscriber: ZZStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>;
        if (from === "beginning") {
          subscriber = stream.subFromBeginning();
        } else if (from === "now") {
          subscriber = stream.subFromNow();
        } else {
          reject(new Error(`Invalid "from" ${from}`));
          return undefined;
        }
        resolve(subscriber);
      });
    });

    return wrapStreamSubscriberWithTermination(subuscriberP);
  }

  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: P;
    bullMQJobsOpts?: JobsOptions;
    inputStreamIdOverridesByTag?: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByTag?: Partial<Record<keyof OMap, string>>;
  }) {
    let {
      jobId = v4(),
      jobOptions,
      bullMQJobsOpts,
      inputStreamIdOverridesByTag,
      outputStreamIdOverridesByTag,
    } = p || {};
    // console.debug("ZZJobSpec._enqueueJob", jobId, jobOptions);
    // force job id to be the same as name
    const workers = await this._rawQueue.getWorkers();
    if (workers.length === 0) {
      this.logger.warn(
        `No worker for queue ${this._rawQueue.name}; job ${jobId} might be be stuck.`
      );
    }
    const projectId = this.zzEnv.projectId;
    if (this.zzEnv.db) {
      await this.zzEnv.db.transaction(async (trx) => {
        await ensureJobAndInitStatusRec({
          projectId,
          specName: this.name,
          jobId,
          dbConn: trx,
          jobOptions,
        });

        if (inputStreamIdOverridesByTag) {
          for (const [key, streamId] of _.entries(
            inputStreamIdOverridesByTag
          )) {
            await ensureStreamRec({
              projectId,
              streamId: streamId as string,
              dbConn: trx,
            });
            await ensureJobStreamConnectorRec({
              projectId,
              streamId: streamId as string,
              dbConn: trx,
              jobId,
              key,
              connectorType: "in",
            });
          }
          this.streamIdOverridesByTagByTypeByJobId[jobId]["in"] =
            inputStreamIdOverridesByTag;
        }

        if (outputStreamIdOverridesByTag) {
          for (const [key, streamId] of _.entries(
            outputStreamIdOverridesByTag
          )) {
            await ensureStreamRec({
              projectId,
              streamId: streamId as string,
              dbConn: trx,
            });
            await ensureJobStreamConnectorRec({
              projectId,
              streamId: streamId as string,
              dbConn: trx,
              jobId,
              key,
              connectorType: "out",
            });
          }
          this.streamIdOverridesByTagByTypeByJobId[jobId]["out"] =
            outputStreamIdOverridesByTag;
        }
      });
    }
    jobOptions = jobOptions || ({} as P);
    const j = await this._rawQueue.add(
      jobId,
      { jobOptions },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID ${j.id} to jobSpec: ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    const input = this._deriveInputsForJob(jobId);
    const output = this._deriveOutputsForJob(jobId);

    return {
      input,
      output,
      jobId,
    };

    // return j;
  }

  public _deriveInputsForJob = (jobId: string) => {
    const that = this;
    return {
      keys: this.inputDefSet.keys,
      feed: async (data: IMap[keyof IMap]) => {
        const tag = that.getSingleInputTag();
        return await that.feedJobInput({
          jobId,
          data,
          tag,
        });
      },
      terminate: async <K extends keyof IMap>(tag?: K) => {
        tag = tag || (that.getSingleInputTag() as K);
        return await that.terminateJobInput({
          jobId,
          tag,
        });
      },

      byTag: <K extends keyof IMap>(key: K) => {
        return {
          feed: async (data: IMap[K]) => {
            return await this.feedJobInput({
              jobId,
              data,
              tag: key,
            });
          },
          terminate: async () => {
            return await this.terminateJobInput({
              jobId,
              tag: key,
            });
          },
        };
      },
    };
  };

  public _deriveOutputsForJob = (jobId: string) => {
    const subscriberByTag = <K extends keyof OMap>(key: K) => {
      const subscriber = this.createOutputCollector({
        jobId,
        key,
      });
      return {
        nextValue: subscriber.nextValue,
        valueObservable: subscriber.valueObservable,
        async *[Symbol.asyncIterator]() {
          while (true) {
            const input = await subscriber.nextValue();

            // Assuming nextInput returns null or a similar value to indicate completion
            if (!input) {
              break;
            }
            yield input;
          }
        },
      };
    };

    let subscriberByDefaultKey: Awaited<
      ReturnType<typeof subscriberByTag>
    > | null = null;

    const nextValue = async <K extends keyof OMap>() => {
      if (subscriberByDefaultKey === null) {
        const key = this.getSingleOutputTag() as K;
        subscriberByDefaultKey = await subscriberByTag(key);
      }
      return await subscriberByDefaultKey.nextValue();
    };

    return {
      keys: this.outputDefSet.keys,
      byTag: <K extends keyof OMap>(key: K) => {
        return subscriberByTag(key);
      },
      nextValue,
      async *[Symbol.asyncIterator]() {
        while (true) {
          const input = await nextValue();

          // Assuming nextInput returns null or a similar value to indicate completion
          if (!input) {
            break;
          }
          yield input;
        }
      },
    };
  };

  // toString
  public toString() {
    return this.name;
  }

  toJSON() {
    return this.name;
  }

  // convenient function
  public defineWorker<WP extends object>(
    p: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec">
  ) {
    return new ZZWorkerDef<P, I, O, WP, IMap, OMap>({
      ...p,
      jobSpec: this,
    });
  }

  public async defineWorkerAndStart<WP extends object>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }

  public static define<P, IMap, OMap>(
    p: ConstructorParameters<typeof ZZJobSpec<P, IMap, OMap>>[0]
  ) {
    return new ZZJobSpec<P, IMap, OMap>(p);
  }
}

export function uniqueStreamIdentifier({
  from,
  to,
}: {
  from?: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
  };
  to?: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
  };
}) {
  const fromStr = !!from
    ? `${from.specName}${
        from.uniqueSpecLabel && from.uniqueSpecLabel !== "default_label"
          ? `[${from.uniqueSpecLabel}]`
          : ""
      }/${from.tag}`
    : "(*)";
  const toStr = !!to
    ? `${to.specName}${
        to.uniqueSpecLabel && to.uniqueSpecLabel !== "default_label"
          ? `[${to.uniqueSpecLabel}]`
          : ""
      }/${to.tag}`
    : "(*)";
  return `${fromStr}>>${toStr}`;
}

export function deriveStreamId({
  groupId,
  from,
  to,
}: { groupId: string } & Parameters<typeof uniqueStreamIdentifier>[0]) {
  const suffix = uniqueStreamIdentifier({ from, to });
  return `stream-${groupId}::${suffix}`;
}

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export const getCachedQueueByName = <JobOptions, JobReturnType>(
  p: {
    projectId: string;
    queueNameOnly: string;
    queueOptions?: WorkerOptions;
  }
  // & {
  //   queueNamesDef: T;
  // }
) => {
  const {
    // queueNamesDef,
    queueNameOnly,
    projectId,
    queueOptions,
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMapByProjectIdAndQueueName.get(
    `${projectId}/${queueNameOnly}`
  ) as Queue<{ jobOptions: JobOptions }, JobReturnType>;
  if (existing) {
    return existing;
  } else {
    const queue = new Queue<{ jobOptions: JobOptions }, JobReturnType>(
      `${projectId}/${queueNameOnly}`,
      queueOptions
    );
    queueMapByProjectIdAndQueueName.set(`${projectId}/${queueNameOnly}`, queue);
    return queue;
  }
};

function wrapIfSingle<
  T,
  TMap = T extends z.ZodType<infer TMap>
    ? {
        default: z.ZodType<TMap>;
      }
    : T extends {
        [key: string]: z.ZodType<any>;
      }
    ? T
    : T extends undefined
    ? {}
    : never
>(def: T): TMap {
  if (!def) {
    return {} as TMap;
  } else if (def instanceof z.ZodType) {
    return {
      default: def!,
    } as TMap;
  } else {
    return def as TMap;
  }
}
