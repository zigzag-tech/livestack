import _ from "lodash";
import { Worker, Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob, ZZProcessor } from "./ZZJob";
import { ZZDuty, getMicroworkerQueueByName } from "./ZZDuty";
import { DutyDef } from "./DutyDef";
import { ZZEnv } from "./ZZEnv";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZodType, z } from "zod";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export class ZZWorkerDef<
  MaDutyDef extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  WP extends object = {}
> {
  public readonly duty: ZZDuty<MaDutyDef>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<MaDutyDef, WP>;

  constructor({
    duty,
    processor,
    instanceParamsDef,
  }: {
    concurrency?: number;
    duty: ZZDuty<MaDutyDef>;
    processor: ZZProcessor<MaDutyDef, WP>;
    instanceParamsDef?: z.ZodType<WP>;
  }) {
    this.duty = duty;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
  }

  public async startWorker(p: { concurrency?: number; instanceParams?: WP }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<MaDutyDef, WP>({
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
  MaDutyDef extends DutyDef<
    any,
    any,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  WP extends object,
  P = z.infer<MaDutyDef["jobParamsDef"]>,
  O extends z.infer<MaDutyDef["outputDef"]> = z.infer<MaDutyDef["outputDef"]>
> {
  public readonly duty: ZZDuty<MaDutyDef>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;

  public readonly bullMQWorker: Worker<
    {
      jobParams: P;
    },
    O | undefined
  >;

  public readonly _rawQueue: IWorkerUtilFuncs<P, O>["_rawQueue"];
  public readonly instanceParams?: WP;
  public readonly workerName: string;
  public readonly def: ZZWorkerDef<MaDutyDef, WP>;
  protected readonly logger: ReturnType<typeof getLogger>;

  constructor({
    instanceParams,
    def,
    workerName,
    concurrency = 3,
  }: {
    def: ZZWorkerDef<MaDutyDef, WP>;
    instanceParams?: WP;
    workerName?: string;
    concurrency?: number;
  }) {
    this.duty = def.duty;
    this.zzEnv = def.duty.zzEnv;
    this.instanceParams = instanceParams;
    this.def = def;

    const workerOptions = {
      autorun: false,
      concurrency,
      connection: this.zzEnv.redisConfig,
    };

    const queueFuncs = getMicroworkerQueueByName<P, O, any>({
      queueNameOnly: `${this.duty.name}`,
      queueOptions: workerOptions,
      db: this.zzEnv.db,
      projectId: this.zzEnv.projectId,
    });

    this._rawQueue = queueFuncs._rawQueue;
    this.workerName =
      workerName || "wkr:" + `${this.zzEnv.projectId}/${workerName}`;
    this.logger = getLogger(`wkr:${this.workerName}`);

    const logger = getLogger(`wkr:${this.workerName}`);
    const mergedWorkerOptions = _.merge({}, workerOptions);

    this.bullMQWorker = new Worker<{ jobParams: P }, O | undefined, string>(
      `${this.zzEnv.projectId}/${this.duty.name}`,
      async (job, token) => {
        const zzJ = new ZZJob<MaDutyDef, WP>({
          bullMQJob: job,
          bullMQToken: token,
          logger,
          duty: this.duty,
          jobParams: job.data.jobParams,
          workerInstanceParams: this.instanceParams,
          storageProvider: this.zzEnv.storageProvider,
          workerName: this.workerName,
        });

        return await zzJ.beginProcessing(this.def.processor.bind(zzJ));
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
    this.bullMQWorker.waitUntilReady().then(() => {
      this.logger.info(`${this.bullMQWorker.name} zzzworker started.`);
    });
  }
}
