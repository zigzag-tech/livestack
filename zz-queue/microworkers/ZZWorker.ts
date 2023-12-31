import _ from "lodash";
import { Worker, Job, FlowProducer } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob, ZZProcessor } from "./ZZJob";
import { ZZPipe, getMicroworkerQueueByName } from "./ZZPipe";
import { InferPipeDef, PipeDef } from "./PipeDef";
import { ZZEnv } from "./ZZEnv";
import { IStorageProvider } from "../storage/cloudStorage";
import { z } from "zod";
import { InferStreamDef } from "./ZZStream";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export class ZZWorker<
  MaPipeDef extends PipeDef<any, any, any, any>,
  WP extends object,
  P = z.infer<InferPipeDef<MaPipeDef>["jobParamsDef"]>,
  O extends InferStreamDef<InferPipeDef<MaPipeDef>["output"]> = InferStreamDef<
    InferPipeDef<MaPipeDef>["output"]
  >
> {
  public readonly pipe: ZZPipe<MaPipeDef>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;

  public readonly bullMQWorker: Worker<
    {
      jobParams: P;
    },
    O | undefined
  >;
  protected color?: string;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];
  public readonly instanceParams: WP;
  public readonly workerName: string;

  constructor({
    pipe,
    zzEnv,
    color,
    concurrency = 3,
    processor,
    instanceParams,
    workerName,
  }: {
    zzEnv: ZZEnv;
    color?: string;
    concurrency?: number;
    pipe: ZZPipe<MaPipeDef>;
    processor: ZZProcessor<MaPipeDef, WP>;
    instanceParams: WP;
    workerName?: string;
  }) {
    this.pipe = pipe;
    this.zzEnv = zzEnv;
    this.instanceParams = instanceParams;

    const workerOptions = {
      autorun: false,
      concurrency,
      connection: this.zzEnv.redisConfig,
    };
    this.color = color;

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueNameOnly: `${this.pipe.name}`,
      queueOptions: workerOptions,
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this._rawQueue = queueFuncs._rawQueue;
    this.workerName = workerName || "wkr:" + this.pipe.name;

    const logger = getLogger(`wkr:${this.workerName}`, this.color);
    const mergedWorkerOptions = _.merge({}, workerOptions);
    const flowProducer = new FlowProducer(mergedWorkerOptions);

    this.bullMQWorker = new Worker<{ jobParams: P }, O | undefined, string>(
      `${this.zzEnv.projectId}/${this.workerName}`,
      async (job, token) => {
        const zzJ = new ZZJob<MaPipeDef, WP>({
          bullMQJob: job,
          bullMQToken: token,
          logger,
          flowProducer,
          pipe: this.pipe,
          jobParams: job.data.jobParams,
          workerInstanceParams: this.instanceParams,
          storageProvider: this.zzEnv.storageProvider,
          workerName: this.workerName,
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
  }
}

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
