import { queue } from "./../../../node_modules/rxjs/src/internal/scheduler/queue";
import { Job, JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { GenericRecordType, QueueName } from "./workerCommon";
import Redis from "ioredis";
import { ZZWorkerDef } from "./ZZWorker";

import {
  ensureJobAndInitStatusRec,
  getJobDataAndIoEvents,
  getJobRec,
} from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";
import {
  InferPipeDef,
  InferPipeInputsDef,
  PipeDef,
  PipeDefParams,
} from "./PipeDef";
import { PubSubFactoryWithNextValueGenerator } from "../realtime/PubSubFactory";
import { z } from "zod";
import { ZZEnv } from "./ZZEnv";
import { InferStreamDef } from "./ZZStream";
export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

export class ZZPipe<
    MaPipeDef extends PipeDef<any, any, any, any>,
    P = z.infer<InferPipeDef<MaPipeDef>["jobParamsDef"]>,
    O extends InferStreamDef<
      InferPipeDef<MaPipeDef>["output"]
    > = InferStreamDef<InferPipeDef<MaPipeDef>["output"]>,
    StreamI extends InferPipeInputsDef<MaPipeDef> = InferPipeInputsDef<MaPipeDef>,
    TProgress = z.infer<InferPipeDef<MaPipeDef>["progressDef"]>
  >
  extends PipeDef<P, O, StreamI, TProgress>
  implements IWorkerUtilFuncs<P, O>
{
  public readonly zzEnv: ZZEnv;
  readonly queueOptions: WorkerOptions;

  protected logger: ReturnType<typeof getLogger>;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];

  private pubSubCache = new Map<
    string,
    PubSubFactoryWithNextValueGenerator<unknown>
  >();

  constructor({
    zzEnv,
    name,
    jobParamsDef,
    output,
    inputs,
    input,
    progressDef,
  }: {
    zzEnv: ZZEnv;
    concurrency?: number;
  } & PipeDefParams<P, O, StreamI, TProgress>) {
    super({
      name,
      jobParamsDef,
      output,
      inputs,
      input,
      progressDef,
    });

    this.queueOptions = {
      connection: zzEnv.redisConfig,
    };

    this.zzEnv = zzEnv;
    this.logger = getLogger(`pipe:${this.name}`);

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
      `Enqueueing job ${jobId} with data: ${JSON.stringify(jobParams)}`
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
    const redis = new Redis();
    redis.del(`last-time-job-alive-${jobId}`);
  }

  public async pingAlive(jobId: string) {
    const redis = new Redis();
    await redis.set(`last-time-job-alive-${jobId}`, Date.now());
  }

  async sendInputToJob({
    jobId,
    data,
    key = "default",
  }: {
    jobId: string;
    data: StreamI;
    key?: string;
  }) {
    try {
      data = this.inputs[key].def.parse(data);
    } catch (err) {
      this.logger.error(`StreamInput data is invalid: ${JSON.stringify(err)}`);
      throw err;
    }
    const pubSub = this.inputs[key];
    const messageId = v4();

    await pubSub.pubToJob({
      data,
      terminate: false,
      __zz_datapoint_id__: messageId,
    });
  }

  async terminateJobInput(jobId: string) {
    const pubSub = this.inputs["default"];
    const messageId = v4();
    await pubSub.pubToJob({
      terminate: true,
      __zz_datapoint_id__: messageId,
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

  public pubSubFactoryForJob = <T>(
    p: {
      jobId: string;
    } & (
      | {
          type: "input";
          key?: string;
        }
      | {
          type: "output";
        }
    )
  ) => {
    const { jobId, type } = p;
    const queueIdPrefix = getPubSubQueueId({
      def: this,
      jobId,
    });
    const queueId =
      type === "input"
        ? `${queueIdPrefix}::${type}/${p.key || "default"}`
        : `${queueIdPrefix}::${type}`;

    let def;

    if (type === "input") {
      def = this.inputs[p.key || "default"].def;
    } else {
      def = this.output.def;
    }
    if (this.pubSubCache.has(`${jobId}/${type}`)) {
      return this.pubSubCache.get(
        queueId
      )! as PubSubFactoryWithNextValueGenerator<WrapTerminatorAndDataId<T>>;
    } else {
      const pubSub = new PubSubFactoryWithNextValueGenerator<
        WrapTerminatorAndDataId<T>
      >({ queueId, def });
      this.pubSubCache.set(`${jobId}/${type}`, pubSub);

      return pubSub;
    }
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
            messageId: msg.__zz_datapoint_id__,
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

export type WrapTerminatorAndDataId<T> = {
  __zz_datapoint_id__: string;
} & (
  | {
      data: T;
      __zz_datapoint_id__: string;
      terminate: false;
    }
  | {
      terminate: true;
    }
);

export function wrapTerminatorAndDataId<T>(t: z.ZodType<T>) {
  return z.union([
    z.object({
      data: t,
      __zz_datapoint_id__: z.string(),
      terminate: z.literal(false),
    }),
    z.object({
      terminate: z.literal(true),
    }),
  ]) as z.ZodType<WrapTerminatorAndDataId<T>>;
}

export function getPubSubQueueId({
  def,
  jobId,
}: {
  def: PipeDef<unknown, unknown, any, unknown>;
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
