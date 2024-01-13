import _ from "lodash";
import { Worker, Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob, ZZProcessor } from "./ZZJob";
import { ZZJobSpec } from "./ZZJobSpec";
import { ZZEnv } from "./ZZEnv";
import { IStorageProvider } from "../storage/cloudStorage";
import { z } from "zod";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

export type ZZWorkerDefParams<
  P,
  IMap,
  OMap,
  TProgress = never,
  WP extends object = {}
> = {
  concurrency?: number;
  jobSpec: ZZJobSpec<P, IMap, OMap, TProgress>;
  processor: ZZProcessor<ZZJobSpec<P, IMap, OMap, TProgress>, WP>;
  instanceParamsDef?: z.ZodType<WP>;
  zzEnv?: ZZEnv;
};

export class ZZWorkerDef<
  P,
  IMap,
  OMap,
  TProgress = never,
  WP extends object = {}
> {
  public readonly jobSpec: ZZJobSpec<P, IMap, OMap, TProgress>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<
    ZZJobSpec<P, IMap, OMap, TProgress>,
    WP
  >;
  public readonly zzEnv: ZZEnv | null = null;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
  }: ZZWorkerDefParams<P, IMap, OMap, TProgress, WP>) {
    this.jobSpec = jobSpec;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
    this.zzEnv = zzEnv || jobSpec.zzEnv;
  }

  public async startWorker(p?: {
    concurrency?: number;
    instanceParams?: WP;
    zzEnv?: ZZEnv;
  }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<P, IMap, OMap, TProgress, WP>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
      zzEnv: p?.zzEnv || this.zzEnv,
    });
    // this.workers.push(worker);
    await worker.waitUntilReady();
    return worker;
  }
  public static define<P, IMap, OMap, TP, WP extends object>(
    p: Omit<ZZWorkerDefParams<P, IMap, OMap, TP, WP>, "jobSpec"> & {
      jobSpec: ConstructorParameters<typeof ZZJobSpec<P, IMap, OMap, TP>>[0];
    }
  ) {
    const spec = new ZZJobSpec<P, IMap, OMap, TP>(p.jobSpec);
    return spec.defineWorker(p);
  }

  public requestJob: (typeof this.jobSpec)["requestJob"] = (p) => {
    return this.jobSpec.requestJob(p);
  };
}

export class ZZWorker<
  P,
  IMap,
  OMap,
  TProgress = never,
  WP extends object = {}
> {
  public readonly jobSpec: ZZJobSpec<P, IMap, OMap, TProgress>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;

  private readonly bullMQWorker: Worker<
    {
      jobParams: P;
    },
    void
  >;

  public readonly instanceParams?: WP;
  public readonly workerName: string;
  public readonly def: ZZWorkerDef<P, IMap, OMap, TProgress, WP>;
  protected readonly logger: ReturnType<typeof getLogger>;

  constructor({
    instanceParams,
    def,
    workerName,
    concurrency = 3,
    zzEnv,
  }: {
    def: ZZWorkerDef<P, IMap, OMap, TProgress, WP>;
    instanceParams?: WP;
    workerName?: string;
    concurrency?: number;
    zzEnv?: ZZEnv | null;
  }) {
    // if worker name is not provided, use random string

    this.jobSpec = def.jobSpec;
    this.zzEnv = zzEnv || def.jobSpec.zzEnv;
    this.instanceParams = instanceParams;
    this.def = def;

    const workerOptions = {
      autorun: false,
      concurrency,
      connection: this.zzEnv.redisConfig,
    };

    this.workerName =
      workerName || "wkr:" + `${this.zzEnv.projectId}/${this.def.jobSpec.name}`;
    this.logger = getLogger(`wkr:${this.workerName}`);

    const mergedWorkerOptions = _.merge({}, workerOptions);
    const that = this;
    this.bullMQWorker = new Worker<{ jobParams: P }, void, string>(
      `${that.zzEnv.projectId}/${this.jobSpec.name}`,
      async (job, token) => {
        const zzJ = new ZZJob({
          bullMQJob: job,
          bullMQToken: token,
          logger: that.logger,
          jobSpec: that.jobSpec,
          jobParams: job.data.jobParams,
          workerInstanceParams: that.instanceParams,
          storageProvider: that.zzEnv.storageProvider,
          workerName: that.workerName,
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
      this.logger.info(`${this.bullMQWorker.name} worker started.`);
    });
  }

  public async waitUntilReady() {
    await this.bullMQWorker.waitUntilReady();
  }
}
