import { Job, JobsOptions, Queue, WorkerOptions, QueueEvents } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZProcessor } from "./ZZJob";
import { ZZWorker } from "./ZZWorker";
import { GenericRecordType, QueueName } from "./workerCommon";
import Redis from "ioredis";

import { addJobRec, getJobData, getJobRec } from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";
import { PipeDef, ZZEnv } from "./PipeRegistry";
import { PubSubFactory } from "../realtime/mq-pub-sub";
import { z } from "zod";
export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMap = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

export class ZZPipe<
  P,
  O,
  StreamI = never,
  WP extends object = {},
  TProgress = never
> implements IWorkerUtilFuncs<P, O>
{
  public def: PipeDef<P, O, StreamI, WP, TProgress>;
  public readonly zzEnv: ZZEnv;
  protected readonly queueOptions: WorkerOptions;
  protected readonly storageProvider?: IStorageProvider;

  protected color?: string;
  protected logger: ReturnType<typeof getLogger>;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];
  // dummy processor
  private processor: ZZProcessor<P, O, StreamI, WP, TProgress> = async (
    job
  ) => {
    throw new Error(`Processor not set!`);
  };

  public async startWorker({
    concurrency,
    instanceParams,
  }: {
    concurrency?: number;
    instanceParams?: WP;
  }) {
    const worker = new ZZWorker<P, O, StreamI, WP, TProgress>({
      zzEnv: this.zzEnv,
      processor: this.processor,
      color: this.color,
      pipe: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
    });
    // this.workers.push(worker);
    await worker.bullMQWorker.waitUntilReady();
    return worker;
  }

  private pubSubCache = new Map<string, PubSubFactory<unknown>>();

  public pubSubFactoryForJob<T>({
    jobId,
    type,
  }: {
    jobId: string;
    type: "input" | "output";
  }) {
    if (this.pubSubCache.has(`${jobId}/${type}`)) {
      return this.pubSubCache.get(`${jobId}/${type}`)! as PubSubFactory<
        WrapTerminatorAndDataId<T>
      >;
    } else {
      const queueId = this.def.name + "::" + jobId;
      const pubSub = new PubSubFactory<WrapTerminatorAndDataId<T>>(
        type,
        {
          projectId: this.zzEnv.projectId,
        },
        this.zzEnv.redisConfig,
        queueId
      );
      this.pubSubCache.set(`${jobId}/${type}`, pubSub);

      return pubSub;
    }
  }

  constructor({
    zzEnv,
    def,
    color,
    processor,
  }: {
    zzEnv: ZZEnv;
    def: PipeDef<P, O, StreamI, WP, TProgress>;
    color?: string;
    concurrency?: number;
    processor: ZZProcessor<P, O, StreamI, WP, TProgress>;
  }) {
    this.def = def;
    this.processor = processor;
    this.queueOptions = {
      connection: zzEnv.redisConfig,
    };
    this.zzEnv = zzEnv;
    this.color = color;
    this.logger = getLogger(`wkr:${this.def.name}`, this.color);

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueName: this.def.name,
      queueOptions: this.queueOptions,
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
      opName: this.def.name,
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
    U = T extends "in" ? StreamI : T extends "out" ? O : P
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
    return await getJobData<U>({
      opName: this.def.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
    });
  }

  public async enqueueJobAndGetResult({
    jobName: jobId,
    initJobParams: initParams,
  }: // queueEventsOptions,
  {
    jobName?: string;
    initJobParams: P;
  }): Promise<O[]> {
    if (!jobId) {
      jobId = `${this.def.name}-${v4()}`;
    }

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(initParams)}`
    );

    await this.addJob({
      jobId,
      initParams,
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
        return results as O[];
      }
    }
  }

  public async cancelLongRunningJob(jobId: string) {
    const job = await this._rawQueue.getJob(jobId);
    if (!job) {
      throw new Error(`Job ${jobId} not found!`);
    }

    // signal to worker to cancel
    const redis = new Redis();
    redis.del(`last-time-job-alive-${jobId}`);
  }

  public async pingAlive(jobId: string) {
    const redis = new Redis();
    await redis.set(`last-time-job-alive-${jobId}`, Date.now());
  }

  public async sendInputToJob({
    jobId,
    data,
  }: {
    jobId: string;
    data: StreamI;
  }) {
    try {
      data = this.def.streamInput.parse(data);
    } catch (err) {
      if (err instanceof z.ZodError) {
        this.logger.error(`StreamInput data is invalid: ${err.message}`);
        throw err;
      }
    }
    const pubSub = this.pubSubFactoryForJob<StreamI>({ jobId, type: "input" });
    const messageId = v4();

    await pubSub.pubToJob({
      messageId,
      message: {
        data,
        terminate: false,
        __zz_job_data_id__: messageId,
      },
    });
  }

  public async terminateJobInput(jobId: string) {
    const pubSub = this.pubSubFactoryForJob({ jobId, type: "input" });
    const messageId = v4();
    await pubSub.pubToJob({
      messageId,
      message: {
        terminate: true,
      },
    });
  }

  public subscribeToJobOutputTillTermination({
    jobId,
    onMessage,
  }: {
    jobId: string;
    onMessage: (
      m:
        | { terminate: false; data: O; messageId: string }
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

  public async subForJobOutput({
    jobId,
    onMessage,
  }: {
    jobId: string;
    onMessage: (
      m:
        | { terminate: false; data: O; messageId: string }
        | {
            terminate: true;
          }
    ) => void;
  }) {
    const pubSub = this.pubSubFactoryForJob<O>({
      jobId,
      type: "output",
    });
    return await pubSub.subForJob({
      processor: (msg) => {
        if (msg.terminate) {
          onMessage(msg);
        } else {
          onMessage({
            data: msg.data,
            terminate: false,
            messageId: msg.__zz_job_data_id__,
          });
        }
      },
    });
  }

  public async nextOutputForJob(jobId: string) {
    const pubSub = this.pubSubFactoryForJob<O>({
      jobId,
      type: "output",
    });
    const v = await pubSub.nextValue();
    if (v.terminate) {
      return null;
    } else {
      return v.data;
    }
  }

  public async addJob({
    jobId,
    initParams,
    bullMQJobsOpts,
  }: {
    jobId: string;
    initParams: P;
    bullMQJobsOpts?: JobsOptions;
  }) {
    // force job id to be the same as name
    const workers = await this._rawQueue.getWorkers();
    if (workers.length === 0) {
      this.logger.warn(`No worker for queue ${this.def.name}.`);
    }
    const j = await this._rawQueue.add(
      jobId,
      { initParams },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID: ${j.id}, ${j.queueName} ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    await addJobRec({
      projectId: this.zzEnv.projectId,
      opName: this.def.name,
      jobId,
      dbConn: this.zzEnv.db,
      initParams: initParams,
    });

    return j;
  }
}

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
    queueName,
    projectId,
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMap.get(`${projectId}/${queueName}`) as ReturnType<
    typeof createAndReturnQueue<JobDataType, JobReturnType, T>
  >;
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
  queueName,
  queueOptions,
  db,
}: {
  projectId: string;
  queueName: QueueName<T>;
  queueOptions?: WorkerOptions;
  db: Knex;
}) {
  const queue = new Queue<{ initParams: JobDataType }, JobReturnType>(
    `${projectId}/${queueName}`,
    queueOptions
  );

  const funcs = {
    _rawQueue: queue,
  };

  // todo: fix typing
  queueMap.set(queueName, funcs as any);

  return funcs;
}

export type WrapTerminatorAndDataId<T> =
  | {
      data: T;
      __zz_job_data_id__: string;
      terminate: false;
    }
  | {
      terminate: true;
    };
