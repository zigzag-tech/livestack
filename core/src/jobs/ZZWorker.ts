import _ from "lodash";
import { getLogger } from "../utils/createWorkerLogger";
import { ZZJob } from "./ZZJob";
import { JobSpec } from "./JobSpec";
import { IStorageProvider } from "../storage/cloudStorage";
import { ZZProcessor } from "./ZZJob";
import { ZZEnv } from "./ZZEnv";
import { z } from "zod";
import { JobId } from "@livestack/shared/src/graph/InstantiatedGraph";
import { resolveInstantiatedGraph } from "../orchestrations/resolveInstantiatedGraph";
import { QueueJob, FromWorker, FromInstance } from "@livestack/vault-interface";
import { v4 } from "uuid";
import { genManuallyFedIterator } from "@livestack/shared";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

export type ZZWorkerDefParams<
  P,
  I,
  O,
  WP extends object | undefined,
  IMap,
  OMap
> = {
  concurrency?: number;
  jobSpec: JobSpec<P, I, O, IMap, OMap>;
  processor: ZZProcessor<P, I, O, WP, IMap, OMap>;
  instanceParamsDef?: z.ZodType<WP>;
  zzEnv?: ZZEnv;
  workerPrefix?: string;
  maxNumWorkers?: number;
};

export class ZZWorkerDef<P, I, O, WP extends object | undefined, IMap, OMap> {
  public readonly jobSpec: JobSpec<P, I, O, IMap, OMap>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<P, I, O, WP, IMap, OMap>;
  public readonly zzEnvP: Promise<ZZEnv>;
  public readonly workerPrefix?: string;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
    workerPrefix,
    maxNumWorkers = 1000,
  }: ZZWorkerDefParams<P, I, O, WP, IMap, OMap>) {
    this.jobSpec = jobSpec;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
    if (zzEnv) {
      this.zzEnvP = Promise.resolve(zzEnv);
    } else {
      this.zzEnvP = this.jobSpec.zzEnvPWithTimeout;
    }

    this.workerPrefix = workerPrefix;

    this.reportInstanceCapacityLazy();
  }

  // public get zzEnv() {
  //   const resolved = this._zzEnv || this.jobSpec.zzEnv || ZZEnv.global();
  //   return resolved;
  // }

  // public get zzEnvEnsured() {
  //   if (!this.zzEnv) {
  //     throw new Error(
  //       `ZZEnv is not configured in Spec ${this.jobSpec.name}. \nPlease either pass it when constructing Spec or set it globally using ZZEnv.setGlobal().`
  //     );
  //   }
  //   return this.zzEnv;
  // }

  private async reportInstanceCapacityLazy() {
    const projectUuid = (await this.zzEnvP).projectUuid;
    const instanceId = await (await this.zzEnvP).getInstanceId();
    const { iterator, resolveNext: reportNext } =
      genManuallyFedIterator<FromInstance>((v) => {
        // console.info(`INSTANCE REPORT: ${JSON.stringify(v)}`);
      });
    const iter = (await this.zzEnvP).vaultClient.capacity.reportAsInstance(
      iterator
    );
    reportNext({
      instanceId,
      projectUuid,
      reportSpecAvailability: {
        specName: this.jobSpec.name,
        maxCapacity: 100,
      },
    });
    for await (const cmd of iter) {
      const { instanceId, provision } = cmd;
      if (instanceId !== (await (await this.zzEnvP).getInstanceId())) {
        throw new Error("Unexpected instanceId");
      }
      if (provision) {
        const { projectUuid, specName, numberOfWorkersNeeded } = provision;
        console.info(
          `Provisioning request received: ${projectUuid}/${specName} for ${numberOfWorkersNeeded}`
        );

        if (specName !== this.jobSpec.name) {
          console.error(
            "Unexpected specName",
            specName,
            "expected",
            this.jobSpec.name
          );
          throw new Error("Unexpected specName");
        }

        for (let i = 0; i < numberOfWorkersNeeded; i++) {
          this.startWorker({
            concurrency: 1,
          });
        }
      } else {
        console.error("Unexpected command", cmd);
        throw new Error("Unexpected command");
      }
    }
  }

  public async startWorker(p?: { concurrency?: number; instanceParams?: WP }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<P, I, O, WP, IMap, OMap>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
      zzEnv: await this.zzEnvP,
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

  public static define<P, I, O, WP extends object | undefined, IMap, OMap>(
    p: Parameters<typeof defineWorker<P, I, O, WP, IMap, OMap>>[0]
  ) {
    return defineWorker(p);
  }
}

function defineWorker<P, I, O, WP extends object | undefined, IMap, OMap>(
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

export class ZZWorker<P, I, O, WP extends object | undefined, IMap, OMap> {
  public readonly jobSpec: JobSpec<P, I, O, IMap, OMap>;
  protected readonly zzEnvP: Promise<ZZEnv>;
  protected readonly storageProvider?: IStorageProvider;
  public readonly instanceParams?: WP;
  public readonly workerNameP: Promise<string>;
  public readonly def: ZZWorkerDef<P, I, O, WP, IMap, OMap>;
  private readonly workerId = v4();
  protected readonly loggerP: Promise<ReturnType<typeof getLogger>>;

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
    if (zzEnv) {
      this.zzEnvP = Promise.resolve(zzEnv);
    } else {
      this.zzEnvP = this.jobSpec.zzEnvP;
    }
    this.instanceParams = instanceParams;
    this.def = def;

    if (workerName) {
      this.workerNameP = Promise.resolve(workerName);
    } else {
      this.workerNameP = this.zzEnvP.then((zzEnv) => {
        return (
          "wkr:" +
          `${zzEnv.projectUuid}/${
            this.def.workerPrefix ? `(${this.def.workerPrefix})` : ""
          }${this.def.jobSpec.name}`
        );
      });
    }

    this.loggerP = this.workerNameP.then((wn) => getLogger(`wkr:${wn}`));
    const that = this;

    // create async iterator to report duty
    const { iterator: iterParams, resolveNext: sendNextActivity } =
      genManuallyFedIterator<FromWorker>((v) => {
        // console.info(`DUTY REPORT: ${JSON.stringify(v)}`);
      });

    (async () => {
      (await that.loggerP).info(`WORKER STARTED: ${await that.workerNameP}.`);
    })();

    // console.info(`WORKER STARTED: ${this.workerName}.`);

    const processJob = async ({ jobId, jobOptionsStr }: QueueJob) => {
      const jobOptions = JSON.parse(jobOptionsStr);
      const localG = await resolveInstantiatedGraph({
        jobId,
        zzEnv: await that.zzEnvP,
        specName: that.jobSpec.name,
      });

      const zzJ = new ZZJob({
        jobId: jobId as JobId,
        logger: await that.loggerP,
        jobSpec: that.jobSpec,
        jobOptions: jobOptions,
        workerInstanceParams: that.instanceParams,
        storageProvider: (await that.zzEnvP).storageProvider,
        workerName: await that.workerNameP,
        graph: localG,
        updateProgress: async (progress): Promise<void> => {
          sendNextActivity({
            progressUpdate: {
              jobId,
              progress,
              projectUuid: (await that.zzEnvP).projectUuid,
              specName: that.jobSpec.name,
            },
            workerId: that.workerId,
          });
        },
      });

      const r = await zzJ.beginProcessing(this.def.processor.bind(zzJ) as any);

      return r;
    };

    this.zzEnvP.then(async (zzEnv) => {
      const iter = zzEnv.vaultClient.queue.reportAsWorker(iterParams);

      for await (const { job } of iter) {
        // console.debug("picked up job: ", job);
        if (!job) {
          throw new Error("Job is null");
        }

        try {
          await processJob(job);
          // console.log("jobCompleted", {
          //   jobId: job.jobId,
          //   projectUuid: that.zzEnv.projectUuid,
          //   specName: that.jobSpec.name,
          // });
          sendNextActivity({
            jobCompleted: {
              jobId: job.jobId,
              projectUuid: (await that.zzEnvP).projectUuid,
              specName: that.jobSpec.name,
            },
            workerId: that.workerId,
          });
          (await that.loggerP).info(`JOB COMPLETED: ${job.jobId}`);
        } catch (err) {
          console.error("jobFailed", err, {
            jobId: job.jobId,
            projectUuid: (await that.zzEnvP).projectUuid,
            specName: that.jobSpec.name,
            errorStr: JSON.stringify(err),
          });
          sendNextActivity({
            jobFailed: {
              jobId: job.jobId,
              projectUuid: (await that.zzEnvP).projectUuid,
              specName: that.jobSpec.name,
              errorStr: JSON.stringify(err),
            },
            workerId: that.workerId,
          });
          (await that.loggerP).error(
            `JOB FAILED: ID: ${job.jobId}, spec: ${that.jobSpec.name}, message: ${err}`
          );
        }
      }
    });

    (async () => {
      // console.debug(
      //   "worker ready to sign up",
      //   that.workerId,
      //   (await that.zzEnvP).projectUuid,
      //   that.jobSpec.name
      // );
      sendNextActivity({
        signUp: {
          projectUuid: (await that.zzEnvP).projectUuid,
          specName: that.jobSpec.name,
        },
        workerId: that.workerId,
      });
    })();
  }

  public static define<P, I, O, WP extends object | undefined, IMap, OMap>(
    p: Parameters<typeof defineWorker<P, I, O, WP, IMap, OMap>>[0]
  ) {
    return defineWorker(p);
  }
}



export type InferDefaultOrSingleKey<T> = "default" extends keyof T
  ? "default"
  : InferSingleKey<T>;

type InferSingleKey<T> = keyof T extends infer U
  ? U extends keyof T
    ? [U] extends [keyof T]
      ? keyof T extends U
        ? U
        : never
      : never
    : never
  : never;

export type InferDefaultOrSingleValue<T> = "default" extends keyof T
  ? T["default"]
  : InferSingleValue<T>;

type InferSingleValue<T> = keyof T extends infer U
  ? U extends keyof T
    ? [U] extends [keyof T]
      ? keyof T extends U
        ? T[U]
        : never
      : never
    : never
  : never;

// type IMap1 = {
//   key1: string;
// };

// type IMap2 = {
//   key1: number;
//   key2: number;
// };

// type IMap3 = {
//   default: string;
// };

// type IMap4 = {
//   default: string;
//   key1: number;
// };

// type SingleKey = InferDefaultOrSingleKey<IMap1>; // true
// type MultipleKeys = InferDefaultOrSingleKey<IMap2>; // false
// type DefaultKey = InferDefaultOrSingleKey<IMap3>; // true
// type DefaultAndOtherKeys = InferDefaultOrSingleKey<IMap4>;
