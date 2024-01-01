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
import {
  InferDutyInputDef,
  InferDutyInputsDef,
  DutyDef,
  DutyDefParams,
} from "./DutyDef";
import { z } from "zod";
import { ZZEnv } from "./ZZEnv";
import { WrapTerminatorAndDataId, wrapTerminatorAndDataId } from "../utils/io";
import { ZZStream } from "./ZZStream";
export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

export class ZZDuty<
    MaDutyDef extends DutyDef<
      unknown,
      unknown,
      Record<string | number | symbol, unknown>,
      unknown,
      unknown
    > = DutyDef<any, any, any, any, any>,
    P = z.infer<MaDutyDef["jobParamsDef"]>,
    O extends z.infer<MaDutyDef["outputDef"]> = z.infer<MaDutyDef["outputDef"]>,
    StreamIMap extends InferDutyInputsDef<MaDutyDef> = InferDutyInputsDef<MaDutyDef>,
    StreamI = InferDutyInputDef<MaDutyDef>,
    TProgress = z.infer<MaDutyDef["progressDef"]>
  >
  extends DutyDef<P, O, StreamIMap, StreamI, TProgress>
  implements IWorkerUtilFuncs<P, O>
{
  public readonly zzEnv: ZZEnv;
  readonly queueOptions: WorkerOptions;

  protected logger: ReturnType<typeof getLogger>;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];

  constructor({
    zzEnv,
    name,
    jobParamsDef,
    outputDef,
    inputDefs,
    inputDef,
    progressDef,
  }: {
    zzEnv: ZZEnv;
    concurrency?: number;
  } & DutyDefParams<P, O, StreamIMap, StreamI, TProgress>) {
    super({
      name,
      jobParamsDef,
      outputDef,
      inputDefs,
      inputDef,
      progressDef,
    });

    this.queueOptions = {
      connection: zzEnv.redisConfig,
    };

    this.zzEnv = zzEnv;
    this.logger = getLogger(`duty:${this.name}`);

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueNameOnly: `${this.name}`,
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
    U = T extends "in" ? StreamIMap : T extends "out" ? O : P
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
  }): Promise<O[]> {
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
    data: StreamIMap[keyof StreamIMap] | StreamI;
    key?: keyof StreamIMap;
  }) {
    try {
      data = this.inputDefs.isSingle
        ? this.inputDefs.def.parse(data)
        : this.inputDefs.defs[key!].parse(data);
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

  async terminateJobInput({
    jobId,
    key,
  }: {
    jobId: string;
    key?: keyof StreamIMap;
  }) {
    const stream = this.getJobStream({ jobId, type: "stream-in", key });
    const messageId = v4();
    await stream.pubToJob({
      terminate: true,
      __zz_datapoint_id__: messageId,
    });
  }

  public async waitForJobReadyForInputs({
    key,
    jobId,
  }: {
    key?: keyof StreamIMap;
    jobId: string;
  }) {
    let isReady = await getJobReadyForInputsInRedis({
      redisConfig: this.zzEnv.redisConfig,
      jobId,
      key: key ? String(key) : "default",
    });
    while (!isReady) {
      await sleep(100);
      isReady = await getJobReadyForInputsInRedis({
        redisConfig: this.zzEnv.redisConfig,
        jobId,
        key: key ? String(key) : "default",
      });
      console.log("isReady", isReady);
    }

    return {
      sendInputToJob: ({
        data,
      }: {
        data: StreamI | StreamIMap[keyof StreamIMap];
      }) => this.sendInputToJob({ jobId, data }),
      terminateJobInput: (p?: { key?: keyof StreamIMap }) =>
        this.terminateJobInput({
          jobId,
          key: p?.key,
        }),
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

  public getJobStream = <T>(
    p: {
      jobId: string;
    } & (
      | {
          type: "stream-in";
          key?: keyof StreamIMap;
        }
      | {
          type: "stream-out";
        }
    )
  ) => {
    const { jobId, type } = p;
    const queueIdPrefix = getPubSubQueueId({
      def: this,
      jobId,
    });
    let queueId: string;
    let def: z.ZodType<WrapTerminatorAndDataId<unknown>>;

    if (type === "stream-in") {
      const origDef = (
        this.inputDefs.isSingle
          ? this.inputDefs.def
          : this.inputDefs.defs[p.key!]
      ) as z.ZodType<unknown>;

      queueId = `${queueIdPrefix}::${type}/${String(p.key || "default")}`;
      def = wrapTerminatorAndDataId(origDef);
    } else if (type === "stream-out") {
      queueId = `${queueIdPrefix}::${type}`;
      def = wrapTerminatorAndDataId(this.outputDef);
    } else {
      throw new Error(`Invalid type ${type}`);
    }

    const stream = ZZStream.getOrCreate({ uniqueName: queueId, def });
    return stream as ZZStream<WrapTerminatorAndDataId<T>>;
  };

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
    const stream = this.getJobStream<O>({
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
    const pubSub = this.getJobStream<O>({
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
      `Added job with ID ${j.id} to duty: ` +
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

export function getPubSubQueueId({
  def,
  jobId,
}: {
  def: DutyDef<unknown, unknown, any, any, unknown>;
  jobId: string;
}) {
  const queueId = def.name + "::" + jobId;
  return queueId;
}

export async function getJobReadyForInputsInRedis({
  redisConfig,
  jobId,
  key,
}: {
  redisConfig: RedisOptions;
  jobId: string;
  key: string;
}) {
  try {
    const redis = new Redis(redisConfig);
    const isReady = await redis.get(`ready_status__${jobId}/${key}`);
    return isReady === "true";
  } catch (error) {
    console.error("Error getJobReadyForInputsInRedis:", error);
  }
}
