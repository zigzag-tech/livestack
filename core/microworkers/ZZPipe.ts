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
import { WrapTerminatorAndDataId, wrapTerminatorAndDataId } from "../utils/io";
import { ZZStream, hashDef } from "./ZZStream";
import { InferStreamSetType, StreamDefSet } from "./StreamDefSet";
import { ZZWorkerDef } from "./ZZWorker";
import { zodToJsonSchema } from "zod-to-json-schema";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof getCachedQueueByName>
>();

// export type CheckTMap<T> = T extends Record<string, infer V> ? T : never;

export type CheckPipe<PP> = PP extends ZZPipe<
  infer P,
  infer I,
  infer O,
  infer TP
>
  ? PP
  : PP extends ZZPipe<infer P, infer I, infer O>
  ? PP
  : never;

export class ZZPipe<P, IMap, OMap, TProgress = never> {
  public readonly zzEnv: ZZEnv;

  protected logger: ReturnType<typeof getLogger>;

  readonly name: string;
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
    jobParamsDef: z.ZodType<P>;
    input: InferDefMap<IMap>;
    output: InferDefMap<OMap>;
    progressDef?: z.ZodType<TProgress>;
    zzEnv?: ZZEnv;
    concurrency?: number;
  }) {
    this.name = name;
    this.jobParamsDef = jobParamsDef;
    this.progressDef = progressDef || z.never();

    this.zzEnv = zzEnv || ZZEnv.global();
    this.logger = getLogger(`pipe:${this.name}`);

    this.inputDefSet = new StreamDefSet({
      defs: input,
    });

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
    return await getJobRec({
      pipeName: this.name,
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
    const rec = await getJobDatapoints<U>({
      pipeName: this.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
      key: String(key),
    });
    return rec.map((r) => r.data);
  }

  public async requestJobAndWaitOnResult({
    jobId: jobId,
    jobParams: jobParams,
    outputKey = "default" as keyof OMap,
  }: // queueEventsOptions,
  {
    jobId?: string;
    jobParams: P;
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
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const status = await this.getJobStatus(jobId);
      if (status === "completed") {
        const results = await this.getJobData({
          jobId,
          ioType: "out",
          limit: 1000,
          key: outputKey,
        });
        return results as OMap[keyof OMap][];
      } else if (status === "failed") {
        throw new Error(`Job ${jobId} failed!`);
      }
    }
  }

  async sendInputToJob({
    jobId,
    data,
    key,
  }: {
    jobId: string;
    data: IMap[keyof IMap];
    key?: keyof IMap;
  }) {
    const stream = await this.getJobStream({ jobId, key, type: "in" });

    await stream.pub({
      message: {
        data,
        terminate: false,
      },
    });
  }

  async terminateJobInput({ jobId, key }: { jobId: string; key?: keyof IMap }) {
    const stream = await this.getJobStream({ jobId, type: "in", key });
    const messageId = v4();
    await stream.pub({
      message: {
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

  public subscribeToJobOutputTillTermination({
    jobId,
    onMessage,
  }: {
    jobId: string;
    onMessage: (
      m:
        | { terminate: false; data: OMap[keyof OMap] }
        | {
            terminate: true;
          }
    ) => void;
  }) {
    return new Promise<void>((resolve, reject) => {
      this.subForJobOutput({
        jobId,
        onMessage: (msg) => {
          if (msg.terminate) {
            resolve();
          }
          onMessage(msg);
        },
      });
    });
  }

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
    key?: keyof IMap | keyof OMap;
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
    if (!overridesByType) {
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

  public async getJobStreamId({
    jobId,
    type,
    p,
  }: {
    jobId: string;
    type: "in" | "out";
    p: {
      key?: keyof IMap | keyof OMap;
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
      const queueIdPrefix = this.name + "::" + jobId;
      const queueId = `${queueIdPrefix}::${type}/${String(p.key || "default")}`;
      streamId = deriveStreamId({
        groupId: queueId,
        ...{
          [type === "in" ? "from" : "to"]: {
            pipe: this,
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
    key?: K;
  }) => {
    const { jobId, type } = p;

    type T = typeof type extends "in" ? IMap[keyof IMap] : OMap[keyof OMap];
    let def: z.ZodType<WrapTerminatorAndDataId<T>>;
    if (type === "in") {
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
    const streamId = await this.getJobStreamId({
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

  public async subForJobOutput({
    jobId,
    onMessage,
  }: {
    jobId: string;
    onMessage: (
      m:
        | { terminate: false; data: OMap[keyof OMap] }
        | {
            terminate: true;
          }
    ) => void;
  }) {
    const stream = await this.getJobStream({
      jobId,
      type: "out",
    });
    return await stream.sub({
      processor: (msg) => {
        if (msg.terminate) {
          onMessage(msg);
        } else {
          onMessage({
            data: msg.data,
            terminate: false,
          });
        }
      },
    });
  }

  public async nextOutputForJob(jobId: string) {
    const pubSub = await this.getJobStream({
      jobId,
      type: "out",
    });
    const v = await pubSub.nextValue();
    if (v.terminate) {
      return null;
    } else {
      return v.data;
    }
  }

  private async _requestJob({
    jobId,
    jobParams,
    bullMQJobsOpts,
    inputStreamIdOverridesByKey,
    outputStreamIdOverridesByKey,
  }: {
    jobId: string;
    jobParams: P;
    bullMQJobsOpts?: JobsOptions;
    inputStreamIdOverridesByKey: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByKey: Partial<Record<keyof OMap, string>>;
  }) {
    // console.debug("ZZPipe._requestJob", jobId, jobParams);
    // force job id to be the same as name
    const workers = await this._rawQueue.getWorkers();
    if (workers.length === 0) {
      this.logger.warn(
        `No worker for queue ${this._rawQueue.name}; job ${jobId} might be be stuck.`
      );
    }
    const projectId = this.zzEnv.projectId;

    await this.zzEnv.db.transaction(async (trx) => {
      await ensureJobAndInitStatusRec({
        projectId,
        pipeName: this.name,
        jobId,
        dbConn: trx,
        jobParams: jobParams,
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

    const j = await this._rawQueue.add(
      jobId,
      { jobParams },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID ${j.id} to pipe: ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    // return j;
  }

  public async requestJob({
    jobId,
    jobParams,
    bullMQJobsOpts,
  }: {
    jobId: string;
    jobParams: P;
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

  public static async requestChainedJobs<Pipes>({
    jobGroupId,
    jobs,
    jobConnectors,
  }: {
    jobGroupId: string;
    jobs: {
      [K in keyof CheckArray<Pipes>]: PipeAndJobParams<CheckArray<Pipes>[K]>;
    };

    jobConnectors: {
      from:
        | ZZPipe<any, any, any, any>
        | [
            ZZPipe<any, any, any, any>,
            (
              | keyof InferStreamSetType<CheckPipe<Pipes>["outputDefSet"]>
              | "default"
            )
          ];

      to:
        | ZZPipe<any, any, any, any>
        | [
            ZZPipe<any, any, any, any>,
            (
              | keyof InferStreamSetType<CheckPipe<Pipes>["outputDefSet"]>
              | "default"
            )
          ];
    }[];
  }) {
    const inOverridesByIndex = [] as {
      [K in keyof CheckArray<Pipes>]: Partial<
        Record<
          keyof CheckPipe<CheckArray<Pipes>[K]>["inputDefSet"]["defs"],
          string
        >
      >;
    };
    const outOverridesByIndex = [] as {
      [K in number]: Partial<
        Record<
          keyof CheckPipe<CheckArray<Pipes>[K]>["outputDefSet"]["defs"],
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

      const fromJobIndex = (jobs as PipeAndJobParams<unknown>[]).findIndex(
        (j) => j.pipe === fromP
      );
      const toJobIndex = (jobs as PipeAndJobParams<unknown>[]).findIndex(
        (j) => j.pipe === toP
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
      if (!fromJobDecs.pipe.outputDefSet.hasDef(fromKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "from" key not found.`
        );
      }
      if (!toJobDesc.pipe.inputDefSet.hasDef(toKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "to" key not found.`
        );
      }

      const fromDef = fromJobDecs.pipe.outputDefSet.getDef(fromKeyStr);
      const toDef = toJobDesc.pipe.inputDefSet.getDef(toKeyStr);
      if (hashDef(fromDef) !== hashDef(toDef)) {
        const msg = `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`;
        console.error(
          msg,
          "Upstream schema: ",
          zodToJsonSchema(fromDef),
          "Downstream schema: ",
          zodToJsonSchema(toDef)
        );
        throw new Error(msg);
      }
      // validate that the types match

      const commonStreamName = deriveStreamId({
        groupId: jobGroupId,
        from: {
          pipe: fromP,
          key: fromKeyStr,
        },
        to: {
          pipe: toP,
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
    const jobIdsByPipeName: { [k: string]: string[] } = {};

    for (let i = 0; i < Object.keys(jobs).length; i++) {
      // calculate overrides based on jobConnectors

      const { pipe, jobParams } = jobs[i];

      if (!countByName[pipe.name]) {
        countByName[pipe.name] = 1;
      } else {
        countByName[pipe.name] += 1;
      }
      const jobId = `[${jobGroupId}]${pipe.name}${
        countByName[pipe.name] > 0 ? `-${countByName[pipe.name]}` : ""
      }`;

      jobIdsByPipeName[pipe.name] = [
        ...(jobIdsByPipeName[pipe.name] || []),
        jobId,
      ];

      const jDef = {
        jobId,
        jobParams,
        inputStreamIdOverridesByKey: inOverridesByIndex[i],
        outputStreamIdOverridesByKey: outOverridesByIndex[i],
      };
      await pipe._requestJob(jDef);
    }
    return { jobIdsByPipeName };
  }

  // toString
  public toString() {
    return this.name;
  }

  // convenient function
  public defineWorker<WP extends object>(
    p: Omit<ZZWorkerDefParams<P, IMap, OMap, TProgress, WP>, "pipe">
  ) {
    return new ZZWorkerDef<P, IMap, OMap, TProgress, WP>({
      ...p,
      pipe: this,
    });
  }

  public async defineAndStartWorker<WP extends object>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, IMap, OMap, TProgress, WP>, "pipe"> & {
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
    pipe: CheckPipe<PP1>;
    key: string;
  };
  to?: {
    pipe: CheckPipe<PP2>;
    key: string;
  };
}) {
  const fromStr = from ? `${from.pipe.name}/${from.key}` : "";
  const toStr = to ? `${to.pipe.name}/${to.key}` : "";
  return `stream-${groupId}::${fromStr}>>${toStr}`;
}

type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

type PipeAndJobParams<Pipe> = {
  pipe: CheckPipe<Pipe>;
  jobParams: z.infer<CheckPipe<Pipe>["jobParamsDef"]>;
  jobLabel?: string;
};

type PipeAndJobOutputKey<PP> =
  | [
      CheckPipe<PP>,
      keyof InferStreamSetType<CheckPipe<PP>["outputDefSet"]> | "default"
    ]
  | CheckPipe<PP>;

type PipeAndJobInputKey<PP> =
  | [
      CheckPipe<PP>,
      keyof InferStreamSetType<CheckPipe<PP>["inputDefSet"]> | "default"
    ]
  | CheckPipe<PP>;

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
