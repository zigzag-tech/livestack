import { Job, JobsOptions, Queue, WorkerOptions, QueueEvents } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZProcessor } from "./ZZJob";
import { ZZWorker } from "./ZZWorker";
import { GenericRecordType, QueueName } from "./workerCommon";
import Redis from "ioredis";

import {
  ensureJobAndInitStatusRec,
  getJobDataAndIoEvents,
  getJobRec,
} from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";
import { PipeDef, ZZEnv } from "./PipeRegistry";
import { PubSubFactoryWithNextValueGenerator } from "../realtime/mq-pub-sub";
import { z } from "zod";
export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMapByProjectIdAndQueueName = new Map<
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
    throw new Error(`Processor for pipe ${this.def.name} not set!`);
  };

  public async startWorker(p?: { concurrency?: number; instanceParams?: WP }) {
    const { concurrency, instanceParams } = p || {};

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
    this.logger.info(`${worker.bullMQWorker.name} worker started.`);
    return worker;
  }

  private pubSubCache = new Map<
    string,
    PubSubFactoryWithNextValueGenerator<unknown>
  >();

  public pubSubFactoryForJob = <T>({
    jobId,
    type,
  }: {
    jobId: string;
    type: "input" | "output";
  }) => {
    if (this.pubSubCache.has(`${jobId}/${type}`)) {
      return this.pubSubCache.get(
        `${jobId}/${type}`
      )! as PubSubFactoryWithNextValueGenerator<WrapTerminatorAndDataId<T>>;
    } else {
      const pubSub = new PubSubFactoryWithNextValueGenerator<
        WrapTerminatorAndDataId<T>
      >(
        type,
        {
          projectId: this.zzEnv.projectId,
        },
        this.zzEnv.redisConfig,
        getPubSubQueueId({
          def: this.def,
          jobId,
        })
      );
      this.pubSubCache.set(`${jobId}/${type}`, pubSub);

      return pubSub;
    }
  };

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
    processor?: ZZProcessor<P, O, StreamI, WP, TProgress>;
  }) {
    this.def = def;
    if (processor) {
      this.processor = processor;
    }
    this.queueOptions = {
      connection: zzEnv.redisConfig,
    };
    this.zzEnv = zzEnv;
    this.color = color;
    this.logger = getLogger(`pipe:${this.def.name}`, this.color);

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueNameOnly: `${this.def.name}`,
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
    const rec = await getJobDataAndIoEvents<U>({
      opName: this.def.name,
      projectId: this.zzEnv.projectId,
      jobId,
      dbConn: this.zzEnv.db,
      ioType,
      order,
      limit,
    });
    return rec.map((r) => r.data);
  }

  public async enqueueJobAndGetResult({
    jobId: jobId,
    initParams: initParams,
  }: // queueEventsOptions,
  {
    jobId?: string;
    initParams: P;
  }): Promise<O[]> {
    if (!jobId) {
      jobId = `${this.def.name}-${v4()}`;
    }

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(initParams)}`
    );

    await this.enqueueJob({
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
    const redis = new Redis();
    redis.del(`last-time-job-alive-${jobId}`);
  }

  public async pingAlive(jobId: string) {
    const redis = new Redis();
    await redis.set(`last-time-job-alive-${jobId}`, Date.now());
  }

  async sendInputToJob({ jobId, data }: { jobId: string; data: StreamI }) {
    try {
      data = this.def.streamInput.parse(data);
    } catch (err) {
      this.logger.error(`StreamInput data is invalid: ${JSON.stringify(err)}`);
      throw err;
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

  async terminateJobInput(jobId: string) {
    const pubSub = this.pubSubFactoryForJob({ jobId, type: "input" });
    const messageId = v4();
    await pubSub.pubToJob({
      messageId,
      message: {
        terminate: true,
      },
    });
  }

  public async waitForJobReadyForInputs(jobId: string) {
    let isReady = await getJobReadyForInputsInRedis(jobId);
    while (!isReady) {
      await sleep(100);
      isReady = await getJobReadyForInputsInRedis(jobId);
      console.log(isReady);
    }

    return {
      sendInputToJob: ({ data }: { data: StreamI }) =>
        this.sendInputToJob({ jobId, data }),
      terminateJobInput: () => this.terminateJobInput(jobId),
    };
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

  public async enqueueJob({
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
      this.logger.warn(
        `No worker for queue ${this._rawQueue.name}; job ${jobId} might be be stuck.`
      );
    }
    const j = await this._rawQueue.add(
      jobId,
      { initParams },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID ${j.id} to pipe: ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    await ensureJobAndInitStatusRec({
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
  const queue = new Queue<{ initParams: JobDataType }, JobReturnType>(
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

export type WrapTerminatorAndDataId<T> =
  | {
      data: T;
      __zz_job_data_id__: string;
      terminate: false;
    }
  | {
      terminate: true;
    };

export function getPubSubQueueId({
  def,
  jobId,
}: {
  def: PipeDef<unknown, unknown, unknown, any, unknown>;
  jobId: string;
}) {
  const queueId = def.name + "::" + jobId;
  return queueId;
}

export async function getJobReadyForInputsInRedis(jobId: string) {
  try {
    const redis = new Redis();
    const isReady = await redis.get(`ready_status__${jobId}`);
    return isReady === "true";
  } catch (error) {
    console.error("Error getJobReadyForInputsInRedis:", error);
  }
}
