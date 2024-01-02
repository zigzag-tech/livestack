import { UnknownTMap } from './StreamDefSet';
import { CheckExtendsDuty } from './ZZJob';
import { InferStreamDef } from './ZZStream';
import { UnknownDuty } from './ZZDuty';
import _ from "lodash";
import { Worker, Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob, ZZProcessor } from "./ZZJob";
import { ZZDuty, getMicroworkerQueueByName } from "./ZZDuty";
import { ZZEnv } from "./ZZEnv";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZodType, z } from "zod";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export class ZZWorkerDef<P,
IMap extends UnknownTMap = UnknownTMap,
OMap extends UnknownTMap = UnknownTMap,
TProgress = never,
WP extends object = {},
> {
  public readonly duty: ZZDuty<P, IMap, OMap, TProgress>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<ZZDuty<P, IMap,OMap, TProgress>, WP>;

  constructor({
    duty,
    processor,
    instanceParamsDef,
  }: {
    concurrency?: number;
    duty: ZZDuty<P, IMap, OMap, TProgress>;
    processor: ZZProcessor<ZZDuty<P, IMap,OMap, TProgress>, WP>;
    instanceParamsDef?: z.ZodType<WP>;
  }) {
    this.duty = duty;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
  }

  public async startWorker(p: { concurrency?: number; instanceParams?: WP }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<P, IMap, OMap, TProgress, WP>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
    });
    // this.workers.push(worker);
    await worker.bullMQWorker.waitUntilReady();
    return worker;
  }
}

export class ZZWorker<
P,
IMap extends UnknownTMap = UnknownTMap,
OMap extends UnknownTMap = UnknownTMap,
TProgress = never,
WP extends object = {},
> {
  public readonly duty:  ZZDuty<P, IMap, OMap, TProgress>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;

  public readonly bullMQWorker: Worker<
    {
      jobParams: P;
    },
    OMap[keyof OMap] | undefined
  >;

  public readonly _rawQueue: IWorkerUtilFuncs<P,  OMap[keyof OMap]>["_rawQueue"];
  public readonly instanceParams?: WP;
  public readonly workerName: string;
  public readonly def: ZZWorkerDef<P, IMap, OMap, TProgress, WP>;
  protected readonly logger: ReturnType<typeof getLogger>;

  constructor({
    instanceParams,
    def,
    workerName,
    concurrency = 3,
  }: {
    def: ZZWorkerDef<P, IMap, OMap, TProgress, WP>;
    instanceParams?: WP;
    workerName?: string;
    concurrency?: number;
  }) {
    // if worker name is not provided, use random string

    this.duty = def.duty;
    this.zzEnv = def.duty.zzEnv;
    this.instanceParams = instanceParams;
    this.def = def;

    const workerOptions = {
      autorun: false,
      concurrency,
      connection: this.zzEnv.redisConfig,
    };

    const queueFuncs = getMicroworkerQueueByName<P,  OMap[keyof OMap], any>({
      queueNameOnly: `${this.duty.name}`,
      queueOptions: workerOptions,
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this._rawQueue = queueFuncs._rawQueue;
    this.workerName =
      workerName || "wkr:" + `${this.zzEnv.projectId}/${this.def.duty.name}`;
    this.logger = getLogger(`wkr:${this.workerName}`);

    const mergedWorkerOptions = _.merge({}, workerOptions);

    this.bullMQWorker = new Worker<{ jobParams: P },  OMap[keyof OMap] | undefined, string>(
      `${this.zzEnv.projectId}/${this.duty.name}`,
      async (job, token) => {
        const zzJ = new ZZJob({
          bullMQJob: job,
          bullMQToken: token,
          logger: this.logger,
          duty: this.duty,
          jobParams: job.data.jobParams,
          workerInstanceParams: this.instanceParams,
          storageProvider: this.zzEnv.storageProvider,
          workerName: this.workerName,
        });

        return await zzJ.beginProcessing(this.def.processor.bind(zzJ) as any);
      },
      mergedWorkerOptions
    );

    // Setup event listeners
    this.bullMQWorker.on("active", (job: Job) => {});

    this.bullMQWorker.on("failed", async (job, error: Error) => {
      this.logger.error(`JOB FAILED: ${job?.id}, ${error}`);
    });

    this.bullMQWorker.on("error", (err) => {
      const errStr = String(err);
      if (!errStr.includes("Missing lock for job")) {
        this.logger.error(`ERROR: ${err}`);
      }
    });

    this.bullMQWorker.on(
      "progress",
      (job: Job, progress: number | object) => {}
    );

    this.bullMQWorker.on("completed", async (job: Job) => {
      this.logger.info(`JOB COMPLETED: ${job.id}`);
    });

    this.bullMQWorker.run();
    this.bullMQWorker.waitUntilReady().then(() => {
      this.logger.info(`${this.bullMQWorker.name} zzzworker started.`);
    });
  }
}
