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

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMap = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

export class ZZPipe<P, O, StreamI = never> implements IWorkerUtilFuncs<P, O> {
  public def: PipeDef<P, O, StreamI>;
  public readonly zzEnv: ZZEnv;
  protected readonly queueOptions: WorkerOptions;
  protected readonly storageProvider?: IStorageProvider;

  // public readonly workers: ZZWorker<P, O, StreamI>[] = [];
  protected color?: string;
  protected logger: ReturnType<typeof getLogger>;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];
  // dummy processor
  private processor: ZZProcessor<P, O, StreamI> = async (job) => {
    throw new Error(`Processor not set!`);
  };

  public async startWorker({ concurrency }: { concurrency?: number }) {
    const worker = new ZZWorker<P, O, StreamI>({
      zzEnv: this.zzEnv,
      processor: this.processor,
      color: this.color,
      pipe: this,
      concurrency,
    });
    // this.workers.push(worker);
    await worker.bullMQWorker.waitUntilReady();
    return worker;
  }

  private pubSubCache = new Map<string, PubSubFactory<unknown>>();

  public pubSubFactoryForJob<T>(jobId: string) {
    if (this.pubSubCache.has(jobId)) {
      return this.pubSubCache.get(jobId)!;
    } else {
      const queueId = this.def.name + "::" + jobId;
      const pubSub = new PubSubFactory<WrapTerminatorAndDataId<T>>(
        {
          projectId: this.zzEnv.projectId,
        },
        this.zzEnv.redisConfig,
        queueId
      );
      this.pubSubCache.set(jobId, pubSub);
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
    def: PipeDef<P, O, StreamI>;
    color?: string;
    concurrency?: number;
    processor?: ZZProcessor<P, O, StreamI>;
  }) {
    this.def = def;
    this.processor = processor || this.processor;
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
    initJobData,
  }: // queueEventsOptions,
  {
    jobName?: string;
    initJobData: P;
  }): Promise<O> {
    if (!jobId) {
      jobId = `${this.def.name}-${v4()}`;
    }

    this.logger.info(`Enqueueing job ${jobId} with data:`, initJobData);
    const queueEvents = new QueueEvents(
      `${this.zzEnv.projectId}/${this.def.name}`,
      {
        connection: this.zzEnv.redisConfig,
      }
    );

    const job = await this.addJob({
      jobId,
      initParams: initJobData,
    });

    try {
      await job.waitUntilFinished(queueEvents);
      const result = await Job.fromId(this._rawQueue, jobId);
      return result!.returnvalue as O;
    } finally {
      await queueEvents.close();
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
    const pubSub = this.pubSubFactoryForJob<StreamI>(jobId);
    const messageId = v4();

    await pubSub.pubToJob({
      type: "input",
      messageId,
      message: {
        data,
        terminate: false,
        __zz_job_data_id__: messageId,
      },
    });
  }

  public async terminateJobInput(jobId: string) {
    const pubSub = this.pubSubFactoryForJob(jobId);
    const messageId = v4();
    await pubSub.pubToJob({
      type: "input",
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
    const pubSub = this.pubSubFactoryForJob(jobId) as PubSubFactory<
      WrapTerminatorAndDataId<O>
    >;
    return await pubSub.subForJob({
      type: "output",
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

  // return queue as Queue<JobDataType, JobReturnType>;
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
