import _ from "lodash";
import { Worker, Job, FlowProducer, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { getMicroworkerQueueByName } from "./queues";
import { Knex } from "knex";

import { IStorageProvider } from "../storage/cloudStorage";
import { RedisOptions } from "ioredis";
import { ZZJob, ZZProcessor } from "./ZZJob";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export abstract class ZZWorker<P, O, StreamI = never>
  implements IWorkerUtilFuncs<P, O>
{
  protected queueName: string;
  protected readonly db: Knex;
  protected readonly workerOptions: WorkerOptions;
  protected readonly storageProvider?: IStorageProvider;

  public readonly bullMQWorker: Worker<
    {
      params: P;
    },
    O
  >;
  protected color?: string;

  public readonly addJob: IWorkerUtilFuncs<P, O>["addJob"];
  public readonly getJob: IWorkerUtilFuncs<P, O>["getJob"];
  public readonly cancelJob: IWorkerUtilFuncs<P, O>["cancelJob"];
  public readonly pingAlive: IWorkerUtilFuncs<P, O>["pingAlive"];
  public readonly getJobData: IWorkerUtilFuncs<P, O>["getJobData"];
  public readonly enqueueJobAndGetResult: IWorkerUtilFuncs<
    P,
    O
  >["enqueueJobAndGetResult"];
  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];

  constructor({
    queueName,
    projectId,
    db,
    redisConfig,
    color,
    storageProvider,
    concurrency = 3,
    processor,
  }: {
    queueName: string;
    projectId: string;
    db: Knex;
    color?: string;
    redisConfig: RedisOptions;
    storageProvider?: IStorageProvider;
    concurrency?: number;
    processor: ZZProcessor<P, O, StreamI>;
  }) {
    this.queueName = queueName;
    this.db = db;
    this.workerOptions = {
      autorun: false,
      concurrency,
      connection: redisConfig,
    };
    this.storageProvider = storageProvider;
    this.color = color;

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueName: this.queueName,
      workerOptions: this.workerOptions,
      db: this.db,
      projectId,
    });

    this.addJob = queueFuncs.addJob;
    this.cancelJob = queueFuncs.cancelJob;
    this.pingAlive = queueFuncs.pingAlive;
    this.enqueueJobAndGetResult = queueFuncs.enqueueJobAndGetResult;
    this._rawQueue = queueFuncs._rawQueue;
    this.getJob = queueFuncs.getJob;
    this.getJobData = queueFuncs.getJobData;

    const logger = getLogger(`wkr:${this.queueName}`, this.color);
    const mergedWorkerOptions = _.merge({}, this.workerOptions);
    const flowProducer = new FlowProducer(mergedWorkerOptions);

    this.bullMQWorker = new Worker<{ params: P }, O, string>(
      this.queueName,
      async (job, token) => {
        const zzJ = new ZZJob<P, O, StreamI>({
          bullMQJob: job,
          bullMQToken: token,
          logger,
          flowProducer,
          projectId,
          queueName: this.queueName,
          storageProvider: this.storageProvider,
          redisConfig,
          processor,
          db,
          params: job.data.params,
        });

        return await zzJ.beginProcessing();
      },
      mergedWorkerOptions
    );

    // Setup event listeners
    this.bullMQWorker.on("active", (job: Job) => {});

    this.bullMQWorker.on("failed", async (job, error: Error) => {
      logger.error(`JOB FAILED: ${job?.id}, ${error}`);
    });

    this.bullMQWorker.on("error", (err) => {
      const errStr = String(err);
      if (!errStr.includes("Missing lock for job")) {
        logger.error(`ERROR: ${err}`);
      }
    });

    this.bullMQWorker.on(
      "progress",
      (job: Job, progress: number | object) => {}
    );

    this.bullMQWorker.on("completed", async (job: Job, result: O) => {
      logger.info(`JOB COMPLETED: ${job.id}`);
    });

    this.bullMQWorker.run();

    logger.info(`${this.queueName} worker started.`);
  }
}

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
