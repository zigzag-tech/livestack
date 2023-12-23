import _ from "lodash";
import { Worker, Job, FlowProducer } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZJob, ZZProcessor } from "./ZZJob";
import { ZZPipe, getMicroworkerQueueByName } from "./ZZPipe";
import { PipeDef, ZZEnv } from "./PipeRegistry";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export class ZZWorker<
  P,
  O,
  StreamI = never,
  WP extends object = never,
  TProgress = never
> {
  public readonly pipe: ZZPipe<P, O, StreamI, WP, TProgress>;
  protected readonly zzEnv: ZZEnv;

  public readonly bullMQWorker: Worker<
    {
      initParams: P;
    },
    O | undefined
  >;
  protected color?: string;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];
  public readonly def: PipeDef<P, O, StreamI, WP, TProgress>;
  private readonly instanceParams?: WP;

  constructor({
    pipe,
    zzEnv,
    color,
    concurrency = 3,
    processor,
    instanceParams,
  }: {
    zzEnv: ZZEnv;
    color?: string;
    storageProvider?: IStorageProvider;
    concurrency?: number;
    pipe: ZZPipe<P, O, StreamI, WP, TProgress>;
    processor: ZZProcessor<P, O, StreamI, WP, TProgress>;
    instanceParams?: WP;
  }) {
    this.pipe = pipe;
    this.zzEnv = zzEnv;
    this.def = pipe.def;
    this.instanceParams = instanceParams;

    const workerOptions = {
      autorun: false,
      concurrency,
      connection: this.zzEnv.redisConfig,
    };
    this.color = color;

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueName: this.def.name,
      queueOptions: workerOptions,
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this._rawQueue = queueFuncs._rawQueue;
    // this.getJobData = queueFuncs.getJobData;

    const logger = getLogger(`wkr:${this.def.name}`, this.color);
    const mergedWorkerOptions = _.merge({}, workerOptions);
    const flowProducer = new FlowProducer(mergedWorkerOptions);

    this.bullMQWorker = new Worker<{ initParams: P }, O | undefined, string>(
      `${this.zzEnv.projectId}/${this.def.name}`,
      async (job, token) => {
        const zzJ = new ZZJob<P, O, StreamI, WP, TProgress>({
          bullMQJob: job,
          bullMQToken: token,
          logger,
          flowProducer,
          pipe: this.pipe,
          initParams: job.data.initParams,
          workerInstanceParams: this.workerInstanceParams,
        });

        return await zzJ.beginProcessing(processor.bind(zzJ));
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

    this.bullMQWorker.on("completed", async (job: Job) => {
      logger.info(`JOB COMPLETED: ${job.id}`);
    });

    this.bullMQWorker.run();
    logger.info(`${this.bullMQWorker.name} worker started.`);
  }
}

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
