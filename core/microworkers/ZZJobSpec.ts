import { ZZWorkerDefParams } from "./ZZWorker";
import { InferDefMap } from "./StreamDefSet";
import { JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { GenericRecordType, QueueName } from "./workerCommon";
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
import { ZZStream, ZZStreamSubscriber, hashDef } from "./ZZStream";
import { InferStreamSetType, StreamDefSet } from "./StreamDefSet";
import { ZZWorkerDef } from "./ZZWorker";

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
  infer TP
>
  ? ZZJobSpec<P, I, O, TP>
  : SP extends ZZJobSpec<infer P, infer I, infer O>
  ? ZZJobSpec<P, I, O>
  : never;

export class ZZJobSpec<P, IMap, OMap, TProgress = never> {
  public readonly zzEnv: ZZEnv;

  protected logger: ReturnType<typeof getLogger>;

  public readonly name: string;
  readonly jobParamsDef: z.ZodType<P>;
  public readonly inputDefSet: StreamDefSet<IMap>;
  public readonly outputDefSet: StreamDefSet<OMap>;
  readonly progressDef: z.ZodType<TProgress>;

  constructor({
    zzEnv,
    name,
    jobParamsDef,
    output,
    input,
    progressDef,
  }: {
    name: string;
    jobParamsDef?: z.ZodType<P>;
    input?: InferDefMap<IMap>;
    output: InferDefMap<OMap>;
    progressDef?: z.ZodType<TProgress>;
    zzEnv?: ZZEnv;
    concurrency?: number;
  }) {
    this.name = name;
    this.jobParamsDef =
      jobParamsDef || (z.object({}) as unknown as z.ZodType<P>);
    this.progressDef = progressDef || z.never();

    this.zzEnv = zzEnv || ZZEnv.global();
    this.logger = getLogger(`spec:${this.name}`);

    if (!input) {
      this.inputDefSet = new StreamDefSet({
        defs: ZZStream.single(z.object({})) as InferDefMap<IMap>,
      });
    } else {
      this.inputDefSet = new StreamDefSet({
        defs: input,
      });
    }
    this.outputDefSet = new StreamDefSet({
      defs: output,
    });
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

  public async requestJobAndWaitOnResult({
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

    await this.requestJob({
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

  async sendInputToJob({
    jobId,
    data,
    key = "default" as keyof IMap,
  }: {
    jobId: string;
    data: IMap[keyof IMap];
    key?: keyof IMap;
  }) {
    const stream = await this.getJobStream({
      jobId,
      key,
      type: "in",
    });

    // const lastV = await stream.lastValue();
    // mark job terminated in redis key value store
    const redis = new Redis(this.zzEnv.redisConfig);
    const isTerminated =
      (await redis.get(`terminated__${jobId}/${String(key)}`)) === "true";
    await redis.disconnect();
    if (isTerminated) {
      this.logger.error(
        `Cannot send input to a terminated stream! jobId: ${jobId}, key: ${String(
          key
        )}`
      );
      throw new Error("Cannot send input to a terminated stream!");
    }

    await stream.pub({
      message: {
        data,
        terminate: false,
      },
    });
  }

  async terminateJobInput({ jobId, key }: { jobId: string; key?: keyof IMap }) {
    const stream = await this.getJobStream({
      jobId,
      type: "in",
      key: key || ("default" as keyof IMap),
    });

    await stream.pub({
      message: {
        terminate: true,
      },
    });

    // mark job terminated in redis key value store
    const redis = new Redis(this.zzEnv.redisConfig);
    // expire in 10 minutes
    await redis.set(`terminated__${jobId}/${String(key)}`, "true", "EX", 600);
    await redis.disconnect();
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
      console.log("isReady", isReady);
    }

    return {
      sendInputToJob: <K extends keyof IMap>({
        data,
        key,
      }: {
        data: IMap[K];
        key?: K;
      }) => this.sendInputToJob({ jobId, data, key }),
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
      console.debug("getJobReadyForInputsInRedis", jobId, key, ":", isReady);
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
      streamId = deriveStreamId({
        groupId: `[${jobId}]`,
        ...{
          [type === "in" ? "to" : "from"]: {
            jobSpec: this,
            key: p.key || "default",
          },
        },
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

  public forJobOutput({
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
          throw new Error(`Invalid "from" ${from}`);
        }
        resolve(subscriber);
      });
    });

    return wrapStreamSubscriberWithTermination(subuscriberP);
  }

  private async _requestJob({
    jobId,
    jobParams,
    bullMQJobsOpts,
    inputStreamIdOverridesByKey,
    outputStreamIdOverridesByKey,
  }: {
    jobId: string;
    jobParams?: P;
    bullMQJobsOpts?: JobsOptions;
    inputStreamIdOverridesByKey: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByKey: Partial<Record<keyof OMap, string>>;
  }) {
    // console.debug("ZZJobSpec._requestJob", jobId, jobParams);
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

    const inputs = this._deriveInputsForJob(jobId);
    const outputs = this._deriveOutputsForJob(jobId);

    return {
      inputs,
      outputs,
    };

    // return j;
  }

  public async requestJob({
    jobId,
    jobParams,
    bullMQJobsOpts,
  }: {
    jobId: string;
    jobParams?: P;
    bullMQJobsOpts?: JobsOptions;
  }) {
    return this._requestJob({
      jobId,
      jobParams,
      bullMQJobsOpts,
      inputStreamIdOverridesByKey: {},
      outputStreamIdOverridesByKey: {},
    });
  }

  public static async requestChainedJobs<Specs>({
    jobGroupId,
    jobs,
    jobConnectors,
  }: {
    jobGroupId: string;
    jobs: {
      [K in keyof CheckArray<Specs>]: JobSpecAndJobParams<CheckArray<Specs>[K]>;
    };

    jobConnectors: {
      from:
        | ZZJobSpec<any, any, any, any>
        | [
            ZZJobSpec<any, any, any, any>,
            (
              | keyof InferStreamSetType<CheckSpec<Specs>["outputDefSet"]>
              | "default"
            )
          ];

      to:
        | ZZJobSpec<any, any, any, any>
        | [
            ZZJobSpec<any, any, any, any>,
            (
              | keyof InferStreamSetType<CheckSpec<Specs>["outputDefSet"]>
              | "default"
            )
          ];
    }[];
  }) {
    const inOverridesByIndex = [] as {
      [K in keyof CheckArray<Specs>]: Partial<
        Record<
          keyof CheckSpec<CheckArray<Specs>[K]>["inputDefSet"]["defs"],
          string
        >
      >;
    };
    const outOverridesByIndex = [] as {
      [K in number]: Partial<
        Record<
          keyof CheckSpec<CheckArray<Specs>[K]>["outputDefSet"]["defs"],
          string
        >
      >;
    };
    // calculate overrides based on jobConnectors
    for (const connector of jobConnectors) {
      const fromPairs = Array.isArray(connector.from)
        ? connector.from
        : ([connector.from, "default"] as const);
      const toPairs = Array.isArray(connector.to)
        ? connector.to
        : ([connector.to, "default"] as const);
      const [fromP] = fromPairs;
      const fromKey =
        fromPairs[1] as keyof (typeof fromP)["outputDefSet"]["defs"];
      const [toP] = toPairs;
      const toKey = toPairs[1] as keyof (typeof toP)["inputDefSet"]["defs"];

      const fromKeyStr = String(fromKey);
      const toKeyStr = String(toKey);

      const fromJobIndex = (jobs as JobSpecAndJobParams<unknown>[]).findIndex(
        (j) => j.spec === fromP
      );
      const toJobIndex = (jobs as JobSpecAndJobParams<unknown>[]).findIndex(
        (j) => j.spec === toP
      );

      if (fromJobIndex === -1) {
        throw new Error(
          `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "from" job not found.`
        );
      }
      const fromJobDecs = jobs[fromJobIndex];
      if (toJobIndex === -1) {
        throw new Error(
          `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "to" job not found.`
        );
      }
      const toJobDesc = jobs[toJobIndex];
      if (!fromJobDecs.spec.outputDefSet.hasDef(fromKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "from" key not found.`
        );
      }
      if (!toJobDesc.spec.inputDefSet.hasDef(toKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "to" key not found.`
        );
      }

      // TODO: to bring back this check
      // const fromDef = fromJobDecs.spec.outputDefSet.getDef(fromKeyStr);
      // const toDef = toJobDesc.spec.inputDefSet.getDef(toKeyStr);
      // if (hashDef(fromDef) !== hashDef(toDef)) {
      //   const msg = `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`;
      //   console.error(
      //     msg,
      //     "Upstream schema: ",
      //     zodToJsonSchema(fromDef),
      //     "Downstream schema: ",
      //     zodToJsonSchema(toDef)
      //   );
      //   throw new Error(msg);
      // }
      // validate that the types match

      const commonStreamName = deriveStreamId({
        groupId: `[${jobGroupId}]`,
        from: {
          jobSpec: fromP,
          key: fromKeyStr,
        },
        to: {
          jobSpec: toP,
          key: toKeyStr,
        },
      });

      inOverridesByIndex[toJobIndex] = {
        ...inOverridesByIndex[toJobIndex],
        [toKeyStr]: commonStreamName,
      };

      outOverridesByIndex[fromJobIndex] = {
        ...outOverridesByIndex[fromJobIndex],
        [fromKeyStr]: commonStreamName,
      };
    }

    // keep count of job with the same name
    const countByName: { [k: string]: number } = {};
    const jobIdsBySpecName: { [k: string]: string[] } = {};

    for (let i = 0; i < Object.keys(jobs).length; i++) {
      // calculate overrides based on jobConnectors

      const { spec: jobSpec, jobParams } = jobs[i];

      if (!countByName[jobSpec.name]) {
        countByName[jobSpec.name] = 0;
      } else {
        countByName[jobSpec.name] += 1;
      }
      const jobId = `[${jobGroupId}]${jobSpec.name}${
        countByName[jobSpec.name] > 0 ? `-${countByName[jobSpec.name]}` : ""
      }`;

      jobIdsBySpecName[jobSpec.name] = [
        ...(jobIdsBySpecName[jobSpec.name] || []),
        jobId,
      ];

      const jDef = {
        jobId,
        jobParams,
        inputStreamIdOverridesByKey: inOverridesByIndex[i],
        outputStreamIdOverridesByKey: outOverridesByIndex[i],
      };
      await jobSpec._requestJob(jDef);
    }

    const identifySingleJobIdBySpec = <P, I, O, TP>(
      spec: ZZJobSpec<P, I, O, TP>
    ) => {
      const jobs = jobIdsBySpecName[spec.name];
      if (jobs.length > 1) {
        throw new Error(
          `More than one job with spec ${spec.name} detected. Please use jobIdsBySpecName instead.`
        );
      }
      const [job] = jobs;
      return job;
    };

    // Create interfaces for inputs and outputs
    const inputs = {
      bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
        return spec._deriveInputsForJob(identifySingleJobIdBySpec(spec));
      },
    };

    const outputs = {
      bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
        return spec._deriveOutputsForJob(identifySingleJobIdBySpec(spec));
      },
    };

    // console.log("countByName", countByName);
    return { jobIdsBySpecName, inputs, outputs };
  }

  private _deriveInputsForJob = (jobId: string) => {
    return {
      send: async ({
        data,
        key = "default" as keyof IMap,
      }: {
        data: IMap[keyof IMap];
        key?: keyof IMap;
      }) => {
        return await this.sendInputToJob({
          jobId,
          data,
          key,
        });
      },
      terminate: async <K extends keyof IMap>(key: K = "default" as K) => {
        return await this.terminateJobInput({
          jobId,
          key,
        });
      },
    };
  };

  private _deriveOutputsForJob = (jobId: string) => {
    const subscriberByKey = <K extends keyof OMap>(key: K) => {
      const subscriber = this.forJobOutput({
        jobId,
        key,
      });
      return subscriber;
    };

    let subscriberByDefaultKey: Awaited<
      ReturnType<typeof subscriberByKey>
    > | null = null;

    return {
      byKey: <K extends keyof OMap>(key: K) => {
        return subscriberByKey(key);
      },
      nextValue: async <K extends keyof OMap>() => {
        if (subscriberByDefaultKey === null) {
          subscriberByDefaultKey = await subscriberByKey("default" as K);
        }
        return await subscriberByDefaultKey.nextValue();
      },
    };
  };

  // toString
  public toString() {
    return this.name;
  }

  // convenient function
  public defineWorker<WP extends object>(
    p: Omit<ZZWorkerDefParams<P, IMap, OMap, TProgress, WP>, "jobSpec">
  ) {
    return new ZZWorkerDef<P, IMap, OMap, TProgress, WP>({
      ...p,
      jobSpec: this,
    });
  }

  public async defineAndStartWorker<WP extends object>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, IMap, OMap, TProgress, WP>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }
}

function deriveStreamId<PP1, PP2>({
  groupId,
  from,
  to,
}: {
  groupId: string;
  from?: {
    jobSpec: CheckSpec<PP1>;
    key: string;
  };
  to?: {
    jobSpec: CheckSpec<PP2>;
    key: string;
  };
}) {
  const fromStr = from ? `${from.jobSpec.name}/${from.key}` : "(*)";
  const toStr = to ? `${to.jobSpec.name}/${to.key}` : "(*)";
  return `stream-${groupId}::${fromStr}>>${toStr}`;
}

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

type JobSpecAndJobParams<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobParams: z.infer<CheckSpec<JobSpec>["jobParamsDef"]>;
  jobLabel?: string;
};

type JobSpecAndJobOutputKey<PP> =
  | [
      CheckSpec<PP>,
      keyof InferStreamSetType<CheckSpec<PP>["outputDefSet"]> | "default"
    ]
  | CheckSpec<PP>;

type JobSpecAndJobInputKey<PP> =
  | [
      CheckSpec<PP>,
      keyof InferStreamSetType<CheckSpec<PP>["inputDefSet"]> | "default"
    ]
  | CheckSpec<PP>;

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
