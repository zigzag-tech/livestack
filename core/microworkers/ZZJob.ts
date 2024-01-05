import { WrapTerminatorAndDataId, wrapTerminatorAndDataId } from "../utils/io";
import { v4 } from "uuid";
import { ZZStream } from "./ZZStream";
import { Job, WaitingChildrenError } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "../realtime/pubsub";
import { Observable, map, takeUntil } from "rxjs";
import {
  IStorageProvider,
  saveLargeFilesToStorage,
} from "../storage/cloudStorage";
import { getTempPathByJobId } from "../storage/temp-dirs";
import path from "path";
import { addDatapoint, getJobRec, updateJobStatus } from "../db/knexConn";
import longStringTruncator from "../utils/longStringTruncator";
import { ZZPipe } from "./ZZPipe";
import { identifyLargeFiles } from "../files/file-ops";
import { z } from "zod";
import Redis, { RedisOptions } from "ioredis";
import { ZZEnv } from "./ZZEnv";

export type ZZProcessor<PP, WP extends object = {}> = PP extends ZZPipe<
  infer P,
  infer IMap,
  infer OMap,
  infer TP
>
  ? (j: ZZJob<P, IMap, OMap, TP, WP>) => Promise<OMap[keyof OMap] | void>
  : never;

export class ZZJob<P, IMap, OMap, TProgress = never, WP extends object = {}> {
  private readonly bullMQJob: Job<{ jobParams: P }, void>;
  public readonly _bullMQToken?: string;
  readonly jobParams: P;
  readonly logger: ReturnType<typeof getLogger>;
  readonly pipe: ZZPipe<P, IMap, OMap, TProgress>;
  // New properties for subscriber tracking

  // public async aliveLoop(retVal: O) {
  //   const redis = new Redis();
  //   let lastTimeJobAlive = Date.now();
  //   while (Date.now() - lastTimeJobAlive < JOB_ALIVE_TIMEOUT) {
  //     lastTimeJobAlive = parseInt(
  //       (await redis.get(`last-time-job-alive-${this.bullMQJob.id}`)) || "0"
  //     );
  //     await sleep(1000 * 60);
  //   }
  //   this.logger.info(
  //     `Job with ${this.jobId} id expired. Marking as complete.`
  //   );
  //   await this.bullMQJob.moveToCompleted(retVal, this._bullMQToken!);
  //   return retVal;
  // }

  emitOutput: (o: OMap[keyof OMap]) => Promise<void>;
  signalOutputEnd: () => Promise<void>;
  nextInput = async <K extends keyof IMap>(key?: K) => {
    await this.setJobReadyForInputsInRedis({
      redisConfig: this.zzEnv.redisConfig,
      jobId: this.jobId,
      isReady: true,
      key: key ? key : "default",
    });
    return await (await this._ensureInputStreamFn(key)).nextValue();
  };

  inputObservableFor = async (key?: keyof IMap) => {
    return await (
      await this._ensureInputStreamFn(key)
    ).trackedObservable;
  };

  // get inputObservable() {
  //   return this.inputObservableFor();
  // }

  dedicatedTempWorkingDir: string;
  baseWorkingRelativePath: string;

  storageProvider?: IStorageProvider;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  public jobId: string;
  private readonly workerName;
  private readonly inputStreamFnsByKey: Partial<{
    [K in keyof IMap]: {
      nextValue: () => Promise<IMap[K] | null>;
      inputStream: ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
      inputObservableUntracked: Observable<IMap[K] | null>;
      trackedObservable: Observable<IMap[K] | null>;
      subscriberCountObservable: Observable<number>;
    };
  }>;

  constructor(p: {
    bullMQJob: Job<{ jobParams: P }, void, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    jobParams: P;
    storageProvider?: IStorageProvider;
    pipe: ZZPipe<P, IMap, OMap, TProgress>;
    workerInstanceParams?: WP;
    workerInstanceParamsSchema?: z.ZodType<WP>;
    workerName: string;
  }) {
    this.bullMQJob = p.bullMQJob;
    this.jobId = this.bullMQJob.id!;
    this._bullMQToken = p.bullMQToken;
    this.logger = p.logger;
    this.workerName = p.workerName;
    this.pipe = p.pipe;

    try {
      this.jobParams = p.pipe.jobParamsDef.parse(p.jobParams) as P;
    } catch (err) {
      this.logger.error(
        `jobParams error: jobParams provided is invalid: ${JSON.stringify(
          err,
          null,
          2
        )}`
      );
      throw err;
    }

    try {
      this.workerInstanceParams = (p.workerInstanceParamsSchema?.parse(
        p.workerInstanceParams
      ) || null) as WP extends object ? WP : null;
    } catch (err) {
      this.logger.error(
        `workerInstanceParams error: data provided is invalid: ${JSON.stringify(
          err
        )}`
      );
      throw err;
    }
    this.storageProvider = p.storageProvider;
    this.zzEnv = p.pipe.zzEnv;
    this.pipe = p.pipe;
    this.baseWorkingRelativePath = path.join(
      this.zzEnv.projectId,
      this.workerName,
      this.jobId!
    );

    const tempWorkingDir = getTempPathByJobId(this.baseWorkingRelativePath);
    this.dedicatedTempWorkingDir = tempWorkingDir;
    this.inputStreamFnsByKey = {};

    this.signalOutputEnd = async () => {
      const outputStream = await this.pipe.getJobStream<OMap[keyof OMap]>({
        jobId: this.jobId,
        type: "stream-out",
      });
      await outputStream.emitValue({
        __zz_datapoint_id__: v4(),
        terminate: true,
      });
    };

    this.emitOutput = async (o: OMap[keyof OMap]) => {
      let { largeFilesToSave, newObj } = identifyLargeFiles(o);

      if (this.storageProvider) {
        const fullPathLargeFilesToSave = largeFilesToSave.map((x) => ({
          ...x,
          path: path.join(this.baseWorkingRelativePath, x.path),
        }));

        if (fullPathLargeFilesToSave.length > 0) {
          this.logger.info(
            `Saving large files to storage: ${fullPathLargeFilesToSave
              .map((x) => x.path)
              .join(", ")}`
          );
          await saveLargeFilesToStorage(
            fullPathLargeFilesToSave,
            this.storageProvider
          );
          o = newObj;
        }
      } else {
        if (largeFilesToSave.length > 0) {
          throw new Error(
            "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
          );
        }
      }

      // this.logger.info(
      //   `Emitting output: ${this.jobId}, ${this.def.name} ` +
      //     JSON.stringify(o, longStringTruncator)
      // );

      const { datapointId } = await addDatapoint({
        projectId: this.zzEnv.projectId,
        dbConn: this.zzEnv.db,
        datapoint: {
          data: o,
          job_id: this.jobId,
          job_output_key: null,
          connector_type: "out",
        },
      });
      const outputStream = await this.pipe.getJobStream<OMap[keyof OMap]>({
        jobId: this.jobId,
        type: "stream-out",
      });
      // update progress to prevent job from being stalling
      await outputStream.emitValue({
        data: o,
        __zz_datapoint_id__: datapointId,
        terminate: false,
      });
      this.bullMQJob.updateProgress(this._dummyProgressCount++);
    };
  }

  private async _ensureInputStreamFn<K extends keyof IMap>(
    key?: K | "default"
  ) {
    if (this.pipe.inputDefSet.isSingle) {
      if (key && key !== "default") {
        throw new Error(
          `inputDefs is single stream, but key is provided: ${String(key)}`
        );
      }
      key = "default" as const;
    } else {
      if (!key) {
        throw new Error(
          `inputDefs is multiple streams, but key is not provided`
        );
      }
    }

    if (!this.inputStreamFnsByKey[key! as keyof IMap]) {
      const stream = (await this.pipe.getJobStream({
        jobId: this.jobId,
        type: "stream-in",
        key: key! as keyof IMap,
      })) as ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
      const inputObservableUntracked = stream.valueObsrvable.pipe(
        map((x) => (x.terminate ? null : x.data))
      );

      const { trackedObservable, subscriberCountObservable } =
        createTrackedObservable(inputObservableUntracked);

      const { nextValue } = createLazyNextValueGenerator(
        inputObservableUntracked
      );

      subscriberCountObservable.subscribe((count) => {
        // console.log("count", count);
        if (count > 0) {
          this.setJobReadyForInputsInRedis({
            redisConfig: this.zzEnv.redisConfig,
            jobId: this.jobId,
            isReady: true,
            key: key || "default",
          });
        }
      });

      this.inputStreamFnsByKey[key as keyof IMap] = {
        nextValue,
        inputStream: stream,
        inputObservableUntracked,
        trackedObservable,
        subscriberCountObservable,
      };
    }
    return this.inputStreamFnsByKey[key as keyof IMap]!;
  }

  setJobReadyForInputsInRedis = async ({
    redisConfig,
    jobId,
    key,
    isReady,
  }: {
    redisConfig: RedisOptions;
    jobId: string;
    key: keyof IMap | "default";
    isReady: boolean;
  }) => {
    console.debug("setJobReadyForInputsInRedis", {
      pipe: this.pipe.name,
      jobId,
      key,
      isReady,
    });
    if (!key) {
      throw new Error("key is required");
    }
    try {
      const redis = new Redis(redisConfig);
      await redis.set(
        `ready_status__${jobId}/${String(key)}`,
        isReady ? "true" : "false"
      );
    } catch (error) {
      console.error("Error setJobReadyForInputsInRedis:", error);
    }
  };

  public beginProcessing = async (
    processor: ZZProcessor<ZZPipe<P, IMap, OMap, TProgress>, WP>
  ): Promise<void> => {
    const jId = {
      pipeName: this.pipe.name,
      jobId: this.jobId,
      projectId: this.zzEnv.projectId,
    };

    const job = this.bullMQJob;
    const logger = this.logger;
    const projectId = this.zzEnv.projectId;

    // const savedResult = await getJobRec<OMap[keyof OMap]>({
    //   ...jId,
    //   dbConn: this.zzEnv.db,
    //   jobStatus: "completed",
    // });
    // if (savedResult) {
    //   const jobData = await getJobDataAndIoEvents<OMap[keyof OMap]>({
    //     ...jId,
    //     dbConn: this.zzEnv.db,
    //     ioType: "out",
    //   });
    //   this.logger.info(
    //     `Job already marked as complete; skipping: ${job.id}, ${this.bullMQJob.queueName} ` +
    //       `${JSON.stringify(this.bullMQJob.data, longStringTruncator)}`,
    //     +`${JSON.stringify(await job.getChildrenValues(), longStringTruncator)}`
    //   );

    //   // return last job data
    //   return jobData[jobData.length - 1]?.data || undefined;
    // } else {
    logger.info(
      `Picked up job: ${job.id}, ${job.queueName} ` +
        `${JSON.stringify(job.data, longStringTruncator)}`,
      +`${JSON.stringify(await job.getChildrenValues(), longStringTruncator)}`
    );

    try {
      await updateJobStatus({
        ...jId,
        dbConn: this.zzEnv.db,
        jobStatus: "running",
      });

      const processedR = await processor(this);

      // wait as long as there are still subscribers

      await Promise.all(
        (Object.values(this.inputStreamFnsByKey) as any).map(
          async (x: (typeof this.inputStreamFnsByKey)[keyof IMap]) => {
            await new Promise<void>((resolve) => {
              x!.subscriberCountObservable
                .pipe(takeUntil(x!.inputObservableUntracked))
                .subscribe((count) => {
                  if (count === 0) {
                    resolve();
                  }
                });
            });
          }
        )
      );

      if (processedR) {
        await this.emitOutput(processedR);
      }

      // await job.updateProgress(processedR as object);
      await updateJobStatus({
        ...jId,
        dbConn: this.zzEnv.db,
        jobStatus: "completed",
      });
      await this.signalOutputEnd();

      // if (processedR) {
      //   return processedR;
      // }
    } catch (e: any) {
      if (e instanceof WaitingChildrenError) {
        await updateJobStatus({
          projectId,
          pipeName: this.pipe.name,
          jobId: job.id!,
          dbConn: this.zzEnv.db,
          jobStatus: "waiting_children",
        });
      } else {
        await updateJobStatus({
          projectId,
          pipeName: this.pipe.name,
          jobId: job.id!,
          dbConn: this.zzEnv.db,
          jobStatus: "failed",
        });
      }
      throw e;
      // }
    }
  };

  // public spawnChildJobsToWaitOn = async <CI, CO>(p: {
  //   def: PipeDef<P, O, StreamIMap, StreamI, TProgress>;
  //   jobId: string;
  //   jobParams: P;
  // }) => {
  //   const spawnR = await this._spawnJob({
  //     ...p,
  //     flowProducerOpts: {
  //       parent: {
  //         id: this.jobId,
  //         queue: this.bullMQJob.queueQualifiedName,
  //       },
  //     },
  //   });

  //   await ensureJobDependencies({
  //     projectId: this.zzEnv.projectId,
  //     parentJobId: this.jobId,
  //     parentPipeName: this.pipe.name,
  //     childJobId: p.jobId,
  //     childPipeName: p.def.name,
  //     dbConn: this.zzEnv.db,
  //     io_event_id: spawnR.ioEventId,
  //   });

  //   return spawnR;
  // };

  // public spawnJob = async <P, O>(p: {
  //   def: PipeDef<P, O, StreamIMap, StreamI, TProgress>;
  //   jobId: string;
  //   jobParams: P;
  // }) => {
  //   return await this._spawnJob(p);
  // };

  // private _spawnJob = async <P, O>({
  //   def: childJobDef,
  //   jobId: childJobId,
  //   jobParams,
  //   flowProducerOpts,
  // }: {
  //   def: PipeDef<P, O, StreamIMap, StreamI, TProgress>;
  //   jobId: string;
  //   jobParams: P;
  //   flowProducerOpts?: FlowJob["opts"];
  // }) => {
  //   const tempPipe = new ZZPipe({
  //     ...childJobDef,
  //     zzEnv: this.zzEnv,
  //   });
  //   await tempPipe.requestJob({
  //     jobId: childJobId,
  //     jobParams,
  //     bullMQJobsOpts: flowProducerOpts,
  //   });
  //   const [rec] = await getJobDataAndIoEvents<P>({
  //     projectId: this.zzEnv.projectId,
  //     pipeName: childJobDef.name,
  //     jobId: childJobId,
  //     dbConn: this.zzEnv.db,
  //     ioType: "init-params",
  //   });

  //   let _outStreamAndFns: {
  //     stream: ZZStream<WrapTerminatorAndDataId<O>>;
  //     nextValue: any;
  //   } | null = null;
  //   const _getOrCreateOutputStream = () => {
  //     if (!_outStreamAndFns) {
  //       const stream = ZZStream.getOrCreate({
  //         uniqueName: `${getPubSubQueueId({
  //           def: childJobDef,
  //           jobId: childJobId,
  //         })}/output`,
  //         def: wrapTerminatorAndDataId(childJobDef.outputDef),
  //       });

  //       const { nextValue } = createLazyNextValueGenerator(
  //         stream.valueObsrvable
  //       );
  //       _outStreamAndFns = {
  //         stream: stream,
  //         nextValue,
  //       };
  //     }

  //     return _outStreamAndFns;
  //   };

  //   return {
  //     get outputObservable() {
  //       return _getOrCreateOutputStream().stream.valueObsrvable.pipe(
  //         map((x) => (x.terminate ? null : x.data))
  //       );
  //     },
  //     nextOutput: async () => {
  //       const r = await _getOrCreateOutputStream().nextValue();
  //       if (r.terminate) {
  //         return null;
  //       } else {
  //         return r.data;
  //       }
  //     },
  //     ...rec,
  //   };
  // };

  // public saveToTextFile = async ({
  //   relativePath,
  //   data,
  // }: {
  //   relativePath: string;
  //   data: string;
  // }) => {
  //   await ensurePathExists(this.dedicatedTempWorkingDir);
  //   fs.writeFileSync(
  //     path.join(this.dedicatedTempWorkingDir, relativePath),
  //     data
  //   );
  // };

  // getLargeValueCdnUrl = async <T extends object>(key: keyof T, obj: T) => {
  //   if (!this.storageProvider) {
  //     throw new Error("storageProvider is not provided");
  //   }
  //   if (!this.storageProvider.getPublicUrl) {
  //     throw new Error("storageProvider.getPublicUrl is not provided");
  //   }
  //   const { largeFilesToSave } = identifyLargeFiles(obj);
  //   const found = largeFilesToSave.find((x) => x.path === key);
  //   if (!found) {
  //     console.error("Available keys: ", Object.keys(obj));
  //     throw new Error(`Cannot find ${String(key)} in largeFilesToSave`);
  //   } else {
  //     return getPublicCdnUrl({
  //       projectId: this.zzEnv.projectId,
  //       jobId: this.jobId,
  //       key: String(key),
  //       storageProvider: this.storageProvider,
  //     });
  //   }
  // };
}
