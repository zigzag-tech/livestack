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
  autostartWorker?: boolean;
};

export class ZZWorkerDef<P, I, O, WP extends object | undefined, IMap, OMap> {
  public readonly jobSpec: JobSpec<P, I, O, IMap, OMap>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<P, I, O, WP, IMap, OMap>;
  public readonly zzEnvP: Promise<ZZEnv>;
  public readonly workerPrefix?: string;

  public static registeredWorkerDefsBySpecName: Record<
    string,
    ZZWorkerDef<any, any, any, any, any, any>
  > = {};

  private static instanceReported = false;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
    workerPrefix,
    maxNumWorkers = 1000,
    autostartWorker = true,
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
    if (autostartWorker === true) {
      this.reportInstanceCapacityLazy();
    }

    if (ZZWorkerDef.registeredWorkerDefsBySpecName[jobSpec.name]) {
      throw new Error(
        `Worker definition for ${jobSpec.name} already exists. Did you define it twice?`
      );
    } else {
      ZZWorkerDef.registeredWorkerDefsBySpecName[jobSpec.name] = this;
    }

    if (!ZZWorkerDef.instanceReported) {
      ZZWorkerDef.instanceReported = true;
      this.reportInstanceCapacityLazy();
    }
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
    const instanceId = await(await this.zzEnvP).getInstanceId();
    // const { iterator: respToVaultIter, resolveNext: reportNext } =
    //   genManuallyFedIterator<FromInstance>((v) => {
    //     // console.info(`INSTANCE REPORT: ${JSON.stringify(v)}`);
    //   });
    const cmdFromVault = (
      await this.zzEnvP
    ).vaultClient.capacity.reportAsInstance({
      projectUuid,
      instanceId,
    });

    // console.debug("Reported capacity for", this.jobSpec.name);
    for await (const cmd of cmdFromVault) {
      const {
        instanceId,
        provision,
        queryCapacity,
        projectUuid,
        correlationId,
      } = cmd;
      if (projectUuid !== (await this.zzEnvP).projectUuid) {
        throw new Error(
          `Unexpected projectUuid. Expected: "${projectUuid}" got "${
            (await this.zzEnvP).projectUuid
          }".`
        );
      }
      if (instanceId !== await(await this.zzEnvP).getInstanceId()) {
        throw new Error(
          `Unexpected instanceId. Expected: "${instanceId}" got "${await(
            await this.zzEnvP
          ).getInstanceId()}".`
        );
      }

      if (queryCapacity) {
        // console.info("Capacity query received: ", queryCapacity);
        const specNames = Object.keys(
          ZZWorkerDef.registeredWorkerDefsBySpecName
        );
        const capacity = 10;

        await(await this.zzEnvP).vaultClient.capacity.respondToCapacityQuery({
          correlationId,
          projectUuid,
          instanceId,
          specNameAndCapacity: specNames.map((specName) => {
            return {
              specName,
              capacity,
            };
          }),
        });
      } else if (provision) {
        const { specName, numberOfWorkersNeeded } = provision;
        console.info(
          `Provisioning request received: ${projectUuid}/${specName} for ${numberOfWorkersNeeded}`
        );

        const workerDef = ZZWorkerDef.registeredWorkerDefsBySpecName[specName];
        if (!workerDef) {
          throw new Error(`Worker definition not found for ${specName}`);
        }

        for (let i = 0; i < numberOfWorkersNeeded; i++) {
          workerDef.startWorker({});
        }

        await(await this.zzEnvP).vaultClient.capacity.respondToProvision({
          correlationId,
          projectUuid,
          instanceId,
          specName,
          numberOfWorkersStarted: numberOfWorkersNeeded,
        });
      } else {
        console.error("Unexpected command", cmd);
        throw new Error("Unexpected command");
      }
    }
  }

  public async startWorker(p?: { instanceParams?: WP }) {
    const { instanceParams } = p || {};

    const worker = new ZZWorker<P, I, O, WP, IMap, OMap>({
      def: this,
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
  protected _workerStatus: "running" | "stopping" | "stopped" = "running";

  constructor({
    instanceParams,
    def,
    workerName,
    zzEnv,
    terminateOutputsOnJobEnd = true,
  }: {
    def: ZZWorkerDef<P, I, O, WP, IMap, OMap>;
    instanceParams?: WP;
    workerName?: string;
    zzEnv?: ZZEnv | null;
    terminateOutputsOnJobEnd?: boolean;
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
    const { iterator: clientMsgIter, resolveNext: sendNextActivity } =
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
        storageProvider: await (await that.zzEnvP).storageProvider,
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

      const r = await zzJ.beginProcessing(this.def.processor.bind(zzJ) as any, {
        terminateOutputsOnJobEnd,
      });

      return r;
    };

    this.zzEnvP.then(async (zzEnv) => {
      while (that._workerStatus === "running") {
        try {
          const serverMsgIter =
            zzEnv.vaultClient.queue.reportAsWorker(clientMsgIter);
          for await (const { job } of serverMsgIter) {
            // console.debug("picked up job: ", job);
            if (!job) {
              throw new Error("Job is null");
            }
            // const gracefulShutdown = async (signal: string) => {
            //   (await that.loggerP).info(
            //     `Received ${signal}. Shutting down gracefully.`
            //   );
            //   sendNextActivity({
            //     jobFailed: {
            //       jobId: job.jobId,
            //       projectUuid: (await that.zzEnvP).projectUuid,
            //       specName: that.jobSpec.name,
            //       errorStr: `${signal} received. Worker shutting down.`,
            //     },
            //     workerId: that.workerId,
            //   });
            //   await new Promise((r) => setTimeout(r, 1000));
            //   process.exit();
            // };

            // process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
            // process.on("SIGINT", () => gracefulShutdown("SIGINT"));

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
            } finally {
              // process.off("SIGTERM", () => gracefulShutdown("SIGTERM"));
              // process.off("SIGINT", () => gracefulShutdown("SIGINT"));
            }
          }
        } catch (e) {
          console.error("Error in reportAsWorker iterator: ", e);
          await new Promise((resolve) => setTimeout(resolve, 5000)); // Wait for 5 seconds before retrying
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

  public async stop() {
    // TODO: Implement graceful shutdown
    throw new Error("Not implemented");
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
