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

  public readonly workers: ZZWorker<P, O, StreamI>[] = [];
  protected color?: string;

  public readonly addJob: IWorkerUtilFuncs<P, O>["addJob"];
  // public readonly getJobData: IWorkerUtilFuncs<P, O>["getJobData"];

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
    this.workers.push(worker);
    return worker;
  }

  private pubSubCache = new Map<
    string,
    PubSubFactory<WrapTerminatorAndDataId<StreamI>, WrapTerminatorAndDataId<O>>
  >();

  public pubSubFactoryForJob(jobId: string) {
    if (this.pubSubCache.has(jobId)) {
      return this.pubSubCache.get(jobId)!;
    } else {
      const pubSub = new PubSubFactory<
        WrapTerminatorAndDataId<StreamI>,
        WrapTerminatorAndDataId<O>
      >(
        {
          projectId: this.zzEnv.projectId,
        },
        this.zzEnv.redisConfig,
        this.def.name + "::" + jobId
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

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueName: this.def.name,
      queueOptions: this.queueOptions,
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this.addJob = queueFuncs.addJob;
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

    console.info(`Enqueueing job ${jobId} with data:`, initJobData);
    const queueEvents = new QueueEvents(this.def.name, {
      connection: this.zzEnv.redisConfig,
    });

    const job = await this.addJob({
      jobId,
      params: initJobData,
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

  public async sendInput({ jobId, data }: { jobId?: string; data: StreamI }) {
    if (!jobId) {
      jobId = `${this.def.name}-${v4()}`;
    }
    const pubSub = this.pubSubFactoryForJob(jobId);
    const messageId = v4();
    await pubSub.pubToJobInput({
      messageId,

      message: {
        data,
        terminate: false,
        __zz_job_data_id__: messageId,
      },
    });
  }

  public async terminateInput(jobId: string) {
    const pubSub = this.pubSubFactoryForJob(jobId);
    const messageId = v4();
    await pubSub.pubToJobInput({
      messageId,
      message: {
        terminate: true,
      },
    });
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
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMap.get(queueName) as ReturnType<
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
  const queue = new Queue<{ params: JobDataType }, JobReturnType>(
    queueName,
    queueOptions
  );

  const logger = getLogger(`wkr:${queueName}`);

  // return queue as Queue<JobDataType, JobReturnType>;
  const addJob = async ({
    jobId,
    params,
    bullMQJobsOpts,
  }: {
    jobId: string;
    params: JobDataType;
    bullMQJobsOpts?: JobsOptions;
  }) => {
    // force job id to be the same as name
    const j = await queue.add(
      jobId,
      { params },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    logger.info(
      `Added job with ID: ${j.id}, ${j.queueName} ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    await addJobRec({
      projectId,
      opName: queueName,
      jobId: j.id!,
      dbConn: db,
      initParams: params,
    });

    return j;
  };

  // const getJobData = async (jobId: string) => {
  //   const j = await queue.getJob(jobId);
  //   if (!j) {
  //     const dbJ = await getJobRec({
  //       opName: queueName,
  //       jobId,
  //       projectId,
  //       dbConn: db,
  //     });
  //     if (dbJ) {
  //       return {
  //         id: dbJ.job_id,
  //         params: dbJ.job_data as { params: JobDataType },
  //       };
  //     }
  //   } else {
  //     return {
  //       id: j.id,
  //       params: j.data.params,
  //     };
  //   }
  // };

  const funcs = {
    addJob,
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
