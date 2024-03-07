import _ from "lodash";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob } from "./ZZJob";
import { JobSpec } from "./JobSpec";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZProcessor } from "./ZZJob";
import { ZZEnv } from "./ZZEnv";
import { z } from "zod";
import { JobId } from "@livestack/shared";
import { resolveInstantiatedGraph } from "../orchestrations/resolveInstantiatedGraph";
import { QueueJob, FromWorker } from "@livestack/vault-interface";
import { v4 } from "uuid";
import { genManuallyFedIterator } from "@livestack/shared";
import { vaultClient } from "@livestack/vault-client";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

export type ZZWorkerDefParams<P, I, O, WP extends object, IMap, OMap> = {
  concurrency?: number;
  jobSpec: JobSpec<P, I, O, IMap, OMap>;
  processor: ZZProcessor<P, I, O, WP, IMap, OMap>;
  instanceParamsDef?: z.ZodType<WP>;
  zzEnv?: ZZEnv;
  workerPrefix?: string;
};

export class ZZWorkerDef<P, I, O, WP extends object, IMap, OMap> {
  public readonly jobSpec: JobSpec<P, I, O, IMap, OMap>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<P, I, O, WP, IMap, OMap>;
  public readonly zzEnv: ZZEnv | null = null;
  public readonly workerPrefix?: string;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
    workerPrefix,
  }: ZZWorkerDefParams<P, I, O, WP, IMap, OMap>) {
    this.jobSpec = jobSpec;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
    this.zzEnv = zzEnv || jobSpec.zzEnv;
    this.workerPrefix = workerPrefix;
  }

  public async startWorker(p?: {
    concurrency?: number;
    instanceParams?: WP;
    zzEnv?: ZZEnv;
  }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<P, I, O, WP, IMap, OMap>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
      zzEnv: p?.zzEnv || this.zzEnv,
    });
    // this.workers.push(worker);
    // await worker.waitUntilReady();
    // console.info("Worker started: ", worker.workerName);
    return worker;
  }

  public enqueueJob: (typeof this.jobSpec)["enqueueJob"] = (p) => {
    return this.jobSpec.enqueueJob(p);
  };

  public enqueueAndGetResult: (typeof this.jobSpec)["enqueueAndGetResult"] = (
    p
  ) => {
    return this.jobSpec.enqueueAndGetResult(p);
  };

  public static define<P, I, O, WP extends object, IMap, OMap>(
    p: Parameters<typeof defineWorker<P, I, O, WP, IMap, OMap>>[0]
  ) {
    return defineWorker(p);
  }
}

function defineWorker<P, I, O, WP extends object, IMap, OMap>(
  p: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec"> & {
    jobSpec: JobSpec<P, I, O, IMap, OMap>;
  }
) {
  const spec =
    p.jobSpec instanceof JobSpec
      ? p.jobSpec
      : (new JobSpec(p.jobSpec) as JobSpec<P, I, O, IMap, OMap>);
  return spec.defineWorker(p);
}

export class ZZWorker<P, I, O, WP extends object, IMap, OMap> {
  public readonly jobSpec: JobSpec<P, I, O, IMap, OMap>;
  protected readonly zzEnv: ZZEnv;
  protected readonly storageProvider?: IStorageProvider;
  public readonly instanceParams?: WP;
  public readonly workerName: string;
  public readonly def: ZZWorkerDef<P, I, O, WP, IMap, OMap>;
  private readonly workerId = v4();
  protected readonly logger: ReturnType<typeof getLogger>;

  constructor({
    instanceParams,
    def,
    workerName,
    concurrency = 3,
    zzEnv,
  }: {
    def: ZZWorkerDef<P, I, O, WP, IMap, OMap>;
    instanceParams?: WP;
    workerName?: string;
    concurrency?: number;
    zzEnv?: ZZEnv | null;
  }) {
    // if worker name is not provided, use random string

    this.jobSpec = def.jobSpec;
    this.zzEnv = zzEnv || def.jobSpec.zzEnvEnsured;
    this.instanceParams = instanceParams;
    this.def = def;

    this.workerName =
      workerName ||
      "wkr:" +
        `${this.zzEnv.projectId}/${
          this.def.workerPrefix ? `(${this.def.workerPrefix})` : ""
        }${this.def.jobSpec.name}`;
    this.logger = getLogger(`wkr:${this.workerName}`);

    const that = this;

    // create async iterator to report duty
    const { iterator: iterParams, resolveNext: resolveNext } =
      genManuallyFedIterator<FromWorker>((v) => {
        // console.info(`DUTY REPORT: ${JSON.stringify(v)}`);
      });
    const iter = vaultClient.queue.reportAsWorker(iterParams);

    this.logger.info(`WORKER STARTED: ${this.workerName}.`);
    // console.info(`WORKER STARTED: ${this.workerName}.`);

    const doIt = async ({ jobId, jobOptionsStr }: QueueJob) => {
      const jobOptions = JSON.parse(jobOptionsStr);
      const localG = await resolveInstantiatedGraph({
        jobId,
        zzEnv: that.zzEnv,
        specName: that.jobSpec.name,
      });

      const zzJ = new ZZJob({
        jobId: jobId as JobId,
        logger: that.logger,
        jobSpec: that.jobSpec,
        jobOptions: jobOptions,
        workerInstanceParams: that.instanceParams,
        storageProvider: that.zzEnv.storageProvider,
        workerName: that.workerName,
        graph: localG,
        updateProgress: async (progress): Promise<void> => {
          resolveNext({
            progressUpdate: {
              jobId,
              progress,
              projectId: that.zzEnv.projectId,
              specName: that.jobSpec.name,
            },
            workerId: that.workerId,
          });
        },
      });

      return await zzJ.beginProcessing(this.def.processor.bind(zzJ) as any);
    };

    (async () => {
      for await (const { job } of iter) {
        // console.log("picked up job: ", job);
        if (!job) {
          throw new Error("Job is null");
        }

        try {
          await doIt(job);
          // console.log("jobCompleted", {
          //   jobId: job.jobId,
          //   projectId: that.zzEnv.projectId,
          //   specName: that.jobSpec.name,
          // });
          resolveNext({
            jobCompleted: {
              jobId: job.jobId,
              projectId: that.zzEnv.projectId,
              specName: that.jobSpec.name,
            },
            workerId: that.workerId,
          });
          that.logger.info(`JOB COMPLETED: ${job.jobId}`);
        } catch (err) {
          console.error("jobFailed", err, {
            jobId: job.jobId,
            projectId: that.zzEnv.projectId,
            specName: that.jobSpec.name,
            errorStr: JSON.stringify(err),
          });
          resolveNext({
            jobFailed: {
              jobId: job.jobId,
              projectId: that.zzEnv.projectId,
              specName: that.jobSpec.name,
              errorStr: JSON.stringify(err),
            },
            workerId: that.workerId,
          });
          that.logger.error(
            `JOB FAILED: ID: ${job.jobId}, spec: ${that.jobSpec.name}, message: ${err}`
          );
        }
      }
    })();
    console.log(
      "worker ready to sign up",
      that.workerId,
      that.zzEnv.projectId,
      that.jobSpec.name
    );
    resolveNext({
      signUp: {
        projectId: that.zzEnv.projectId,
        specName: that.jobSpec.name,
      },
      workerId: that.workerId,
    });
  }

  public static define<P, I, O, WP extends object, IMap, OMap>(
    p: Parameters<typeof defineWorker<P, I, O, WP, IMap, OMap>>[0]
  ) {
    return defineWorker(p);
  }
}
