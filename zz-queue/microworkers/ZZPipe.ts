import { ZZWorkerDefParams } from "./ZZWorker";
import { InferDefMap } from "./StreamDefSet";
import { JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { GenericRecordType, QueueName } from "./workerCommon";
import Redis from "ioredis";

import {
  ensureJobAndInitStatusRec,
  getJobDataAndIoEvents,
  getJobRec,
} from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";

import { z } from "zod";
import { ZZEnv } from "./ZZEnv";
import { WrapTerminatorAndDataId, wrapTerminatorAndDataId } from "../utils/io";
import { ZZStream, hashDef } from "./ZZStream";
import { InferStreamSetType, StreamDefSet } from "./StreamDefSet";
import { ZZWorkerDef } from "./ZZWorker";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
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

export class ZZPipe<P, IMap, OMap, TProgress = never>
  implements IWorkerUtilFuncs<P, OMap[keyof OMap]>
{
  public readonly zzEnv: ZZEnv;

  protected logger: ReturnType<typeof getLogger>;
  public readonly _rawQueue: IWorkerUtilFuncs<P, OMap[keyof OMap]>["_rawQueue"];

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

    const queueFuncs = getMicroworkerQueueByName<P, OMap[keyof OMap], any>({
      queueNameOnly: `${this.name}`,
      queueOptions: {
        connection: this.zzEnv.redisConfig,
      },
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this._rawQueue = queueFuncs._rawQueue;
    // this.getJobData = queueFuncs.getJobData;
  }

  // public async getJob(jobId: string) {
  //   const j = await this._rawQueue.getJob(jobId);
  //   return j || null;
  // }

  public async getJobRec(jobId: string) {
    return await getJobRec({
      opName: this.name,
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
    T extends "in" | "out" | "init-params",
    U = T extends "in"
      ? IMap[keyof IMap]
      : T extends "out"
      ? OMap[keyof OMap]
      : P
  >({
    jobId,
    ioType,
    order,
    limit,
  }: {
    jobId: string;
    ioType: T;
    order?: "asc" | "desc";
    limit?: number;
  }) {
    const rec = await getJobDataAndIoEvents<U>({
      opName: this.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
    });
    return rec.map((r) => r.data);
  }

  public async requestJobAndWaitOnResult({
    jobId: jobId,
    jobParams: jobParams,
  }: // queueEventsOptions,
  {
    jobId?: string;
    jobParams: P;
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
        });
        return results as OMap[keyof OMap][];
      } else if (status === "failed") {
        throw new Error(`Job ${jobId} failed!`);
      }
    }
  }

  public async cancelLongRunningJob(jobId: string) {
    const job = await this._rawQueue.getJob(jobId);
    if (!job) {
      throw new Error(`Job ${jobId} not found!`);
    }

    // signal to worker to cancel
    const redis = new Redis(this.zzEnv.redisConfig);
    redis.del(`last-time-job-alive-${jobId}`);
  }

  public async pingAlive(jobId: string) {
    const redis = new Redis(this.zzEnv.redisConfig);
    await redis.set(`last-time-job-alive-${jobId}`, Date.now());
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
    const stream = this.getJobStream({ jobId, key, type: "stream-in" });
    const messageId = v4();

    await stream.pubToJob({
      data,
      terminate: false,
      __zz_datapoint_id__: messageId,
    });
  }

  async terminateJobInput({ jobId, key }: { jobId: string; key?: keyof IMap }) {
    const stream = this.getJobStream({ jobId, type: "stream-in", key });
    const messageId = v4();
    await stream.pubToJob({
      terminate: true,
      __zz_datapoint_id__: messageId,
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
        | { terminate: false; data: OMap[keyof OMap]; messageId: string }
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

  public getPubSubQueueId({ jobId }: { jobId: string }) {
    const queueId = this.name + "::" + jobId;
    return queueId;
  }

  public getJobStream = <
    T,
    KI extends keyof IMap = keyof IMap,
    KO extends keyof OMap = keyof OMap
  >(
    p: {
      jobId: string;
    } & (
      | {
          type: "stream-in";
          key?: KI;
        }
      | {
          type: "stream-out";
          key?: KO;
        }
    )
  ) => {
    const { jobId, type } = p;
    const queueIdPrefix = this.getPubSubQueueId({
      jobId,
    });
    let queueId: string;
    let def: z.ZodType<WrapTerminatorAndDataId<unknown>>;

    if (type === "stream-in") {
      const origDef = this.inputDefSet.getDef(p.key) as z.ZodType<unknown>;

      queueId = `${queueIdPrefix}::${type}/${String(p.key || "default")}`;
      def = wrapTerminatorAndDataId(origDef);
    } else if (type === "stream-out") {
      queueId = `${queueIdPrefix}::${type}`;
      def = wrapTerminatorAndDataId(
        this.outputDefSet.getDef(p.key) as z.ZodType<unknown>
      );
    } else {
      throw new Error(`Invalid type ${type}`);
    }

    const stream = ZZStream.getOrCreate({
      uniqueName: queueId,
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
        | { terminate: false; data: OMap[keyof OMap]; messageId: string }
        | {
            terminate: true;
          }
    ) => void;
  }) {
    const stream = this.getJobStream<OMap[keyof OMap]>({
      jobId,
      type: "stream-out",
    });
    return await stream.sub({
      processor: (msg) => {
        if (msg.terminate) {
          onMessage(msg);
        } else {
          onMessage({
            data: msg.data,
            terminate: false,
            messageId: msg.__zz_datapoint_id__,
          });
        }
      },
    });
  }

  public async nextOutputForJob(jobId: string) {
    const pubSub = this.getJobStream<OMap[keyof OMap]>({
      jobId,
      type: "stream-out",
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
    inputStreamKeyIdOverrides,
    outputStreamKeyIdOverrides,
  }: {
    jobId: string;
    jobParams: P;
    bullMQJobsOpts?: JobsOptions;
    inputStreamKeyIdOverrides: Partial<Record<keyof IMap, string>>;
    outputStreamKeyIdOverrides: Partial<Record<keyof OMap, string>>;
  }) {
    // force job id to be the same as name
    const workers = await this._rawQueue.getWorkers();
    if (workers.length === 0) {
      this.logger.warn(
        `No worker for queue ${this._rawQueue.name}; job ${jobId} might be be stuck.`
      );
    }

    const j = await this._rawQueue.add(
      jobId,
      { jobParams },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID ${j.id} to pipe: ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    await ensureJobAndInitStatusRec({
      projectId: this.zzEnv.projectId,
      opName: this.name,
      jobId,
      dbConn: this.zzEnv.db,
      jobParams: jobParams,
    });

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
      inputStreamKeyIdOverrides: {},
      outputStreamKeyIdOverrides: {},
    });
  }

  public static async requestChainedJob<Pipes>({
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
        throw new Error(
          `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`
        );
      }
      // validate that the types match

      const commonStreamName = getStreamId({
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

    for (let i = 0; i < Object.keys(jobs).length; i++) {
      // calculate overrides based on jobConnectors

      const { pipe, jobParams } = jobs[i];
      const jobId = `${jobGroupId}-${i}`;

      const jDef = {
        jobId,
        jobParams,
        inputStreamKeyIdOverrides: inOverridesByIndex[i],
        outputStreamKeyIdOverrides: outOverridesByIndex[i],
      };
      const job = await pipe._requestJob(jDef);
    }
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
}

function getStreamId<PP1, PP2>({
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

export const getMicroworkerQueueByName = <
  JobDataType,
  JobReturnType,
  T extends GenericRecordType
>(
  p: Parameters<typeof createAndReturnQueue<JobDataType, JobReturnType, T>>[0]
  // & {
  //   queueNamesDef: T;
  // }
): ReturnType<typeof createAndReturnQueue<JobDataType, JobReturnType, T>> => {
  const {
    // queueNamesDef,
    queueNameOnly: queueName,
    projectId,
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMapByProjectIdAndQueueName.get(
    `${projectId}/${queueName}`
  ) as ReturnType<typeof createAndReturnQueue<JobDataType, JobReturnType, T>>;
  if (existing) {
    return existing;
  } else {
    return createAndReturnQueue<JobDataType, JobReturnType, T>(p);
  }
};

function createAndReturnQueue<
  JobDataType,
  JobReturnType,
  T extends GenericRecordType = GenericRecordType
>({
  projectId,
  queueNameOnly,
  queueOptions,
  db,
}: {
  projectId: string;
  queueNameOnly: QueueName<T>;
  queueOptions?: WorkerOptions;
  db: Knex;
}) {
  const queue = new Queue<{ jobParams: JobDataType }, JobReturnType>(
    `${projectId}/${queueNameOnly}`,
    queueOptions
  );

  const funcs = {
    _rawQueue: queue,
  };

  // todo: fix typing
  queueMapByProjectIdAndQueueName.set(
    `${projectId}/${queueNameOnly}`,
    funcs as any
  );

  return funcs;
}
