import { ZZWorkerDefParams } from "./ZZWorker";
import {
  InferDefMap,
  InferStreamSetType,
} from "@livestack/shared/StreamDefSet";
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

import { z } from "zod";
import { ZZEnv } from "./ZZEnv";
import {
  WrapTerminatorAndDataId,
  wrapStreamSubscriberWithTermination,
  wrapTerminatorAndDataId,
} from "../utils/io";
import { ZZStream, ZZStreamSubscriber } from "./ZZStream";
import { ZZWorkerDef } from "./ZZWorker";
import pLimit from "p-limit";
import { convertSpecOrName } from "../orchestrations/ZZWorkflow";
import { ZZJobSpecBase } from "@livestack/shared/ZZJobSpecBase";
import { SpecOrName } from "@livestack/shared/ZZJobSpecBase";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof getCachedQueueByName>
>();

// export type CheckTMap<T> = T extends Record<string, infer V> ? T : never;

export type CheckSpec<SP> = SP extends ZZJobSpec<infer P, infer I, infer O>
  ? ZZJobSpec<P, I, O>
  : never;

export type InferOutputType<
  Spec,
  K = "default",
  OMap = InferStreamSetType<CheckSpec<Spec>["outputDefSet"]>
> = OMap[K extends keyof OMap ? K : never] | null;

export type InferInputType<
  Spec,
  K = "default",
  IMap = InferStreamSetType<CheckSpec<Spec>["inputDefSet"]>
> = IMap[K extends keyof IMap ? K : never] | null;

export class ZZJobSpec<
  P,
  IMap = {
    default: {};
  },
  OMap = {
    default: {};
  }
> extends ZZJobSpecBase<P, IMap, OMap> {
  private readonly _zzEnv: ZZEnv | null = null;
  protected static _registryBySpecName: Record<
    string,
    ZZJobSpecBase<any, any, any>
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

  readonly jobParams: z.ZodType<P>;

  constructor({
    zzEnv,
    name,
    jobParams,
    output,
    input,
  }: {
    name: string;
    jobParams?: z.ZodType<P>;
    input?: InferDefMap<IMap>;
    output?: InferDefMap<OMap>;
    zzEnv?: ZZEnv;
    concurrency?: number;
  }) {
    super({
      name,
      input,
      output,
    });

    this.jobParams = jobParams || (z.object({}) as unknown as z.ZodType<P>);
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

  public inputDef(key: keyof IMap = "default" as keyof IMap) {
    return this.inputDefSet.getDef(key);
  }

  public outputDef(key: keyof OMap = "default" as keyof OMap) {
    return this.outputDefSet.getDef(key);
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
      ...this,
      input: this.input,
      output: this.output,
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
    U = T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
  >({
    jobId,
    ioType = "out" as T,
    order,
    limit,
    key = "default" as keyof (T extends "in" ? IMap : OMap),
  }: {
    jobId: string;
    ioType?: T;
    order?: "asc" | "desc";
    limit?: number;
    key?: keyof (T extends "in" ? IMap : OMap);
  }) {
    if (!this.zzEnv.db) {
      throw new Error(
        "Database is not configured in ZZEnv, therefore can not get job data."
      );
    }
    const recs = await getJobDatapoints<WrapTerminatorAndDataId<U>>({
      specName: this.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
      key: String(key),
    });
    return recs
      .map((r) => r.data)
      .filter((r) => r.terminate === false)
      .map((r) =>
        r.terminate === false ? r.data : null
      ) as OMap[keyof OMap][];
  }

  public async enqueueJobAndWaitOnResults({
    jobId: jobId,
    jobParams: jobParams,
    outputKey = "default" as keyof OMap,
  }: // queueEventsOptions,
  {
    jobId?: string;
    jobParams?: P;
    outputKey?: keyof OMap;
  }): Promise<OMap[keyof OMap][]> {
    if (!jobId) {
      jobId = `${this.name}-${v4()}`;
    }

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(jobParams)}.`
    );

    await this.enqueueJob({
      jobId,
      jobParams,
    });

    while (true) {
      await sleep(200);
      const status = await this.getJobStatus(jobId);
      if (status === "completed") {
        const results = await this.getJobData({
          jobId,
          ioType: "out",
          limit: 1000,
          key: outputKey,
        });
        return results;
      } else if (status === "failed") {
        throw new Error(`Job ${jobId} failed!`);
      }
    }
  }

  async feedJobInput({
    jobId,
    data,
    key = "default" as keyof IMap,
  }: {
    jobId: string;
    data: IMap[keyof IMap];
    key?: keyof IMap;
  }) {
    await this._getStreamAndSendDataToPLimited({
      jobId,
      key,
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
    key,
    type,
    data: d,
  }: {
    jobId: string;
    key: T extends "in" ? keyof IMap : keyof OMap;
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

    if (!dict[key]) {
      dict[key] = pLimit(1);
    }

    const limit = dict[key]!;

    await limit(async () => {
      // console.debug("stream_data", JSON.stringify(d, longStringTruncator));

      const redis = new Redis(this.zzEnv.redisConfig);
      if (!d.terminate) {
        // const lastV = await stream.lastValue();
        // mark job terminated in redis key value store
        const isTerminated =
          (await redis.get(`terminated__${jobId}/${type}/${String(key)}`)) ===
          "true";
        await redis.disconnect();
        if (isTerminated) {
          this.logger.error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream! jobId: ${jobId}, key: ${String(key)}`
          );
          throw new Error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream!`
          );
        }
      } else {
        await redis.set(
          `terminated__${jobId}/${type}/${String(key)}`,
          "true",
          "EX",
          600
        );
      }
      await redis.disconnect();

      const stream = await this.getJobStream({
        jobId,
        key,
        type,
      });
      await stream.pub({
        message: d,
        ...(type === "out"
          ? {
              jobInfo: {
                jobId,
                jobOutputKey: String(key),
              },
            }
          : {}),
      });
    });
  }

  async terminateJobInput({ jobId, key }: { jobId: string; key?: keyof IMap }) {
    await this._getStreamAndSendDataToPLimited({
      jobId,
      key: key || ("default" as keyof IMap),
      type: "in",
      data: {
        terminate: true,
      },
    });
  }

  public async waitForJobReadyForInputs({
    jobId,
    key,
  }: {
    jobId: string;
    key?: keyof IMap;
  }) {
    let isReady = await this.getJobReadyForInputsInRedis({
      jobId,
      key: key ? key : ("default" as keyof IMap),
    });
    while (!isReady) {
      await sleep(100);
      isReady = await this.getJobReadyForInputsInRedis({
        jobId,
        key: key ? key : ("default" as keyof IMap),
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
      }) => this.feedJobInput({ jobId, data, key }),
      terminateJobInput: <K extends keyof IMap>(p?: { key?: K }) =>
        this.terminateJobInput({
          jobId,
          key: p?.key,
        }),
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

  private streamIdOverridesByKeyByTypeByJobId: {
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
    const overridesById = this.streamIdOverridesByKeyByTypeByJobId[jobId];
    if (!overridesById) {
      this.streamIdOverridesByKeyByTypeByJobId[jobId] = {
        in: null,
        out: null,
      };
    }

    const overridesByType =
      this.streamIdOverridesByKeyByTypeByJobId[jobId][type];
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
      this.streamIdOverridesByKeyByTypeByJobId[jobId][type] =
        overrides as Partial<Record<keyof IMap | keyof OMap, string>>;
    }

    return (
      ((
        this.streamIdOverridesByKeyByTypeByJobId[jobId][type]! as Record<
          any,
          string
        >
      )[(key || "default") as keyof IMap | keyof OMap] as string | undefined) ||
      null
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
      key: keyof IMap | keyof OMap;
    };
  }) {
    let streamId: string;
    const streamIdOverride = await this.getStreamIdOverride({
      jobId,
      type,
      key: p.key,
    });
    if (streamIdOverride) {
      streamId = streamIdOverride;
    } else {
      // const queueIdPrefix = ;
      // const queueId = `${type}/${String(p.key || "default")}`;
      const dir = type === "in" ? "to" : "from";
      streamId = deriveStreamId({
        groupId: `[${jobId}]`,
        from:
          type === "in"
            ? undefined
            : { specName: this.name, key: String(p.key) },
        to:
          type === "out"
            ? undefined
            : { specName: this.name, key: String(p.key) },
      });
    }
    return streamId;
  }

  public getJobStream = async <
    TT extends "in" | "out",
    K extends TT extends "in" ? keyof IMap : keyof OMap
  >(p: {
    jobId: string;
    type: TT;
    key: K;
  }) => {
    const { jobId, type } = p;

    type T = typeof type extends "in" ? IMap[keyof IMap] : OMap[keyof OMap];
    let def: z.ZodType<WrapTerminatorAndDataId<T>>;
    if (type === "in") {
      if (!this.inputDefSet) {
        throw new Error(`No input defined for job spec ${this.name}`);
      }
      def = wrapTerminatorAndDataId(
        this.inputDefSet.getDef(p.key as keyof IMap)
      ) as z.ZodType<WrapTerminatorAndDataId<T>>;
    } else if (type === "out") {
      def = wrapTerminatorAndDataId(
        this.outputDefSet.getDef(p.key as keyof OMap)
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

  public createOutputCollector({
    jobId,
    key,
    from = "beginning",
  }: {
    jobId: string;
    key?: keyof OMap;
    from?: "beginning" | "now";
  }) {
    const subuscriberP = new Promise<
      ZZStreamSubscriber<WrapTerminatorAndDataId<OMap[keyof OMap]>>
    >((resolve, reject) => {
      this.getJobStream({
        jobId,
        type: "out",
        key: key || ("default" as keyof OMap),
      }).then((stream) => {
        let subscriber: ZZStreamSubscriber<
          WrapTerminatorAndDataId<OMap[keyof OMap]>
        >;
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
    jobParams?: P;
    bullMQJobsOpts?: JobsOptions;
    inputStreamIdOverridesByKey?: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByKey?: Partial<Record<keyof OMap, string>>;
  }) {
    let {
      jobId = v4(),
      jobParams,
      bullMQJobsOpts,
      inputStreamIdOverridesByKey,
      outputStreamIdOverridesByKey,
    } = p || {};
    // console.debug("ZZJobSpec._enqueueJob", jobId, jobParams);
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
          jobParams,
        });

        for (const [key, streamId] of _.entries(inputStreamIdOverridesByKey)) {
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

        for (const [key, streamId] of _.entries(outputStreamIdOverridesByKey)) {
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
      });
    }
    jobParams = jobParams || ({} as P);
    const j = await this._rawQueue.add(
      jobId,
      { jobParams },
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

  public async enqueueJobAndGetOutputs(
    p: Parameters<ZZJobSpec<P, IMap, OMap>["enqueueJob"]>[0] & {
      key?: keyof OMap;
    }
  ) {
    const { jobId, jobParams, key } = p;
    const { output } = await this.enqueueJob({
      jobId,
      jobParams,
    });

    const rs: OMap[keyof OMap][] = [];
    let lastV: OMap[keyof OMap] | null = null;
    while ((lastV = await output.nextValue()) !== null) {
      rs.push(lastV);
    }
    return rs;
  }

  public _deriveInputsForJob = (jobId: string) => {
    return {
      keys: this.inputDefSet.keys,
      feed: async (data: IMap[keyof IMap]) => {
        if (!this.inputDefSet.hasDef("default")) {
          throw new Error(
            `There are more than one input keys defined for job ${this.name}, please use jobSpec.byKey({key}).feed() instead.`
          );
        }
        return await this.feedJobInput({
          jobId,
          data,
          key: "default" as keyof IMap,
        });
      },
      terminate: async <K extends keyof IMap>(key: K = "default" as K) => {
        if (!this.inputDefSet.hasDef("default")) {
          throw new Error(
            `There are more than one input keys defined for job ${this.name}, please use jobSpec.byKey({key}).terminate() instead.`
          );
        }
        return await this.terminateJobInput({
          jobId,
          key,
        });
      },

      byKey: <K extends keyof IMap>(key: K) => {
        return {
          feed: async (data: IMap[K]) => {
            return await this.feedJobInput({
              jobId,
              data,
              key,
            });
          },
          terminate: async () => {
            return await this.terminateJobInput({
              jobId,
              key,
            });
          },
        };
      },
    };
  };

  public _deriveOutputsForJob = (jobId: string) => {
    const subscriberByKey = <K extends keyof OMap>(key: K) => {
      const subscriber = this.createOutputCollector({
        jobId,
        key,
      });
      return subscriber;
    };

    let subscriberByDefaultKey: Awaited<
      ReturnType<typeof subscriberByKey>
    > | null = null;

    const nextValue = async <K extends keyof OMap>() => {
      if (subscriberByDefaultKey === null) {
        subscriberByDefaultKey = await subscriberByKey("default" as K);
      }
      return await subscriberByDefaultKey.nextValue();
    };

    return {
      keys: this.outputDefSet.keys,
      byKey: <K extends keyof OMap>(key: K) => {
        return subscriberByKey(key);
      },
      nextValue,
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
    p: Omit<ZZWorkerDefParams<P, IMap, OMap, WP>, "jobSpec">
  ) {
    return new ZZWorkerDef<P, IMap, OMap, WP>({
      ...p,
      jobSpec: this,
    });
  }

  public async defineWorkerAndStart<WP extends object>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, IMap, OMap, WP>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }
}

export function uniqueStreamIdentifier({
  from,
  to,
}: {
  from?: {
    specName: string;
    key: string;
    uniqueLabel?: string;
  };
  to?: {
    specName: string;
    key: string;
    uniqueLabel?: string;
  };
}) {
  const fromStr = !!from
    ? `${from.specName}${
        from.uniqueLabel && from.uniqueLabel !== "default_label"
          ? `[${from.uniqueLabel}]`
          : ""
      }/${from.key}`
    : "(*)";
  const toStr = !!to
    ? `${to.specName}${
        to.uniqueLabel && to.uniqueLabel !== "default_label"
          ? `[${to.uniqueLabel}]`
          : ""
      }/${to.key}`
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

export const getCachedQueueByName = <JobParams, JobReturnType>(
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
  ) as Queue<{ jobParams: JobParams }, JobReturnType>;
  if (existing) {
    return existing;
  } else {
    const queue = new Queue<{ jobParams: JobParams }, JobReturnType>(
      `${projectId}/${queueNameOnly}`,
      queueOptions
    );
    queueMapByProjectIdAndQueueName.set(`${projectId}/${queueNameOnly}`, queue);
    return queue;
  }
};
export const UniqueSpecQuery = z.union([
  SpecOrName,
  z.object({
    spec: SpecOrName,
    label: z.string().default("default_label"),
  }),
]);
export type UniqueSpecQuery = z.infer<typeof UniqueSpecQuery>;
export const SpecAndOutlet = z.union([
  UniqueSpecQuery,
  z.tuple([UniqueSpecQuery, z.string().or(z.literal("default"))]),
]);
export type SpecAndOutlet = z.infer<typeof SpecAndOutlet>;
export function resolveUniqueSpec(uniqueSpec: UniqueSpecQuery): {
  specName: string;
  uniqueLabel?: string;
} {
  if (typeof uniqueSpec === "string") {
    return {
      specName: uniqueSpec,
    };
  } else if ("spec" in (uniqueSpec as any) && "label" in (uniqueSpec as any)) {
    return {
      specName: convertSpecOrName(
        (
          uniqueSpec as {
            spec: SpecOrName;
            label: string;
          }
        ).spec
      ),
      uniqueLabel: (
        uniqueSpec as {
          spec: SpecOrName;
          label: string;
        }
      ).label,
    };
  } else {
    return {
      specName: convertSpecOrName(uniqueSpec as any),
    };
  }
}
