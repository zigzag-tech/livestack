import { JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { GenericRecordType, QueueName } from "./workerCommon";
import Redis, { RedisOptions } from "ioredis";

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
import { ZZStream } from "./ZZStream";
import { InferStreamSetType, StreamDefSet, UnknownTMap } from "./StreamDefSet";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

type InferZodTypeMap<TMap extends UnknownTMap> = {
  [K in keyof TMap]: z.ZodType<TMap[K]>;
};

export type UnknownPipe = ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>;

export class ZZPipe<
  P,
  IMap extends UnknownTMap = UnknownTMap,
  OMap extends UnknownTMap = UnknownTMap,
  TProgress = never
> implements IWorkerUtilFuncs<P, OMap[keyof OMap]>
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
    input: InferZodTypeMap<IMap>;
    output: InferZodTypeMap<OMap>;
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
    U = T extends "in" ? IMap : T extends "out" ? OMap[keyof OMap] : P
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
    try {
      const def = this.inputDefSet.getDef(key);
      data = def.parse(data);
    } catch (err) {
      this.logger.error(`StreamInput data is invalid: ${JSON.stringify(err)}`);
      throw err;
    }
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
      key: key ? key : "default",
    });
    while (!isReady) {
      await sleep(100);
      isReady = await this.getJobReadyForInputsInRedis({
        jobId,
        key: key ? key : "default",
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
      terminateJobInput: (p?: { key?: keyof IMap }) =>
        this.terminateJobInput({
          jobId,
          key: p?.key,
        }),
    };
  }

  getJobReadyForInputsInRedis = async ({
    jobId,
    key,
  }: {
    jobId: string;
    key: keyof IMap | "default";
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

  public getJobStream = <T>(
    p: {
      jobId: string;
    } & (
      | {
          type: "stream-in";
          key?: keyof IMap | "default";
        }
      | {
          type: "stream-out";
          key?: keyof OMap | "default";
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

  public async requestJob({
    jobId,
    jobParams,
    bullMQJobsOpts,
  }: {
    jobId: string;
    jobParams: P;
    bullMQJobsOpts?: JobsOptions;
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

    return j;
  }

  public static async requestComplexJob<
    Ps extends {
      [K in number]: any;
    },
    IMaps extends {
      [K in keyof Ps]: Record<any, unknown>;
    },
    OMaps extends {
      [K in keyof Ps]: Record<any, unknown>;
    },
    TProgresses extends { [K in keyof Ps]: any }
  >({
    jobGroupId,
    pipes,
  }: {
    jobGroupId: string;
    pipes: {
      [K in keyof Ps]: PipeAndJobParams<K, Ps, IMaps, OMaps, TProgresses>;
    };
    streamConnectors: {
      from:
        | PipeAndJobParams<number, Ps, IMaps, OMaps, TProgresses>["pipe"]
        | PipeAndJobOutputKey<number, Ps, IMaps, OMaps, TProgresses>;
      to:
        | PipeAndJobParams<number, Ps, IMaps, OMaps, TProgresses>["pipe"]
        | PipeAndJobInputKey<number, Ps, IMaps, OMaps, TProgresses>;
    }[];
  }) {}
}

type PipeAndJobParams<
  K extends keyof Ps,
  Ps extends {
    [K in number]: any;
  },
  IMaps extends {
    [K in keyof Ps]: UnknownTMap;
  },
  OMaps extends {
    [K in keyof Ps]: Record<any, unknown>;
  },
  TProgresses extends { [K in keyof Ps]: any },
  P = Ps[K],
  IMap extends UnknownTMap = IMaps[K],
  OMap extends UnknownTMap = OMaps[K],
  TProgress = TProgresses[K]
> = {
  pipe: ZZPipe<P, IMap, OMap, TProgress>;
  jobParams: P;
  outputKey?: keyof InferStreamSetType<ZZPipe<P, IMap, OMap>["outputDefSet"]>;
};

type PipeAndJobOutputKey<
  K extends keyof Ps,
  Ps extends {
    [K in number]: any;
  },
  IMaps extends {
    [K in keyof Ps]: UnknownTMap;
  },
  OMaps extends {
    [K in keyof Ps]: Record<any, unknown>;
  },
  TProgresses extends { [K in keyof Ps]: any },
  P = Ps[K],
  IMap extends UnknownTMap = IMaps[K],
  OMap extends UnknownTMap = OMaps[K],
  TProgress = TProgresses[K]
> = [
  ZZPipe<P, IMap, OMap, TProgress>,
  keyof InferStreamSetType<ZZPipe<P, IMap, OMap>["inputDefSet"]>
];

type PipeAndJobInputKey<
  K extends keyof Ps,
  Ps extends {
    [K in number]: any;
  },
  IMaps extends {
    [K in keyof Ps]: UnknownTMap;
  },
  OMaps extends {
    [K in keyof Ps]: Record<any, unknown>;
  },
  TProgresses extends { [K in keyof Ps]: any },
  P = Ps[K],
  IMap extends UnknownTMap = IMaps[K],
  OMap extends UnknownTMap = OMaps[K],
  TProgress = TProgresses[K]
> = [
  ZZPipe<P, IMap, OMap, TProgress>,
  keyof ZZPipe<P, IMap, OMap>["inputDefSet"]
];

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

type A<T> = {
  a: {
    b: T;
  };
};

const a1: A<string> = {
  a: {
    b: "1",
  },
};
const a2: A<number> = {
  a: {
    b: 1,
  },
};

type S<T> = {
  s: {
    a: T;
  };
};

const s1: S<string> = {
  s: {
    a: "1",
  },
};
const s2: S<number> = {
  s: {
    a: 1,
  },
};

function someFn<T>({ pairs }: { pairs: Array<{ a: A<T>; s: S<T> }> }) {
  // function implementation
}
