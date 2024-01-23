import _ from "lodash";
import { Worker, Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob } from "./ZZJob";
import { ZZJobSpec } from "./ZZJobSpec";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZProcessor } from "./ZZJob";
import { ZZEnv } from "./ZZEnv";
import { z } from "zod";


export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;


export type ZZWorkerDefParams<P, IMap, OMap, WP extends object = {}> = {
  concurrency?: number;
  jobSpec: ZZJobSpec<P, IMap, OMap>;
  processor: ZZProcessor<ZZJobSpec<P, IMap, OMap>, WP>;
  instanceParamsDef?: z.ZodType<WP>;
  zzEnv?: ZZEnv;
};

export class ZZWorkerDef<P, IMap = {}, OMap = {}, WP extends object = {}> {
  public readonly jobSpec: ZZJobSpec<P, IMap, OMap>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<ZZJobSpec<P, IMap, OMap>, WP>;
  public readonly zzEnv: ZZEnv | null = null;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
  }: ZZWorkerDefParams<P, IMap, OMap, WP>) {
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

    const worker = new ZZWorker<P, IMap, OMap, WP>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
      zzEnv: p?.zzEnv || this.zzEnv,
    });
    // this.workers.push(worker);
    await worker.waitUntilReady();
    return worker;
  }

  public enqueueJob: (typeof this.jobSpec)["enqueueJob"] = (p) => {
    return this.jobSpec.enqueueJob(p);
  };

  public static define<P, IMap, OMap, WP extends object>(
    p: Parameters<typeof defineWorker>[0]
  ) {
    return defineWorker(p);
  }
}

function defineWorker<P, IMap, OMap, WP extends object>(
  p: Omit<ZZWorkerDefParams<P, IMap, OMap, WP>, "jobSpec"> & {
    jobSpec:
      | ConstructorParameters<typeof ZZJobSpec<P, IMap, OMap>>[0]
      | ZZJobSpec<P, IMap, OMap>;
  }
) {
  const spec = new ZZJobSpec<P, IMap, OMap>(p.jobSpec);
  return spec.defineWorker(p);
}


export class ZZWorker<P, IMap, OMap, WP extends object = {}> {
  public readonly jobSpec: ZZJobSpec<P, IMap, OMap>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;

  private readonly bullMQWorker: Worker<
    {
      jobOptions: P;
    },
    void
  >;

  public readonly instanceParams?: WP;
  public readonly workerName: string;
  public readonly def: ZZWorkerDef<P, IMap, OMap, WP>;
  protected readonly logger: ReturnType<typeof getLogger>;

  constructor({
    instanceParams,
    def,
    workerName,
    concurrency = 3,
    zzEnv,
  }: {
    def: ZZWorkerDef<P, IMap, OMap, WP>;
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
    this.bullMQWorker = new Worker<{ jobOptions: P }, void, string>(
      `${that.zzEnv.projectId}/${this.jobSpec.name}`,
      async (job, token) => {
        const zzJ = new ZZJob({
          bullMQJob: job,
          bullMQToken: token,
          logger: that.logger,
          jobSpec: that.jobSpec,
          jobOptions: job.data.jobOptions,
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
      this.logger.info(`WORKER STARTED: ${this.workerName}.`);
    });
  }

  public async waitUntilReady() {
    await this.bullMQWorker.waitUntilReady();
  }

  public static define<P, IMap, OMap, WP extends object>(
    p: Parameters<typeof defineWorker>[0]
  ) {
    return defineWorker(p);
  }
}
