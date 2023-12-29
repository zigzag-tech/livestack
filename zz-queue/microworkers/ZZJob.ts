import { Job, FlowJob, FlowProducer, WaitingChildrenError } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  PubSubFactory,
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "../realtime/mq-pub-sub";
import {
  BehaviorSubject,
  Observable,
  Subject,
  Subscriber,
  Subscription,
  map,
  takeUntil,
  tap,
} from "rxjs";
import {
  IStorageProvider,
  getPublicCdnUrl,
  saveLargeFilesToStorage,
} from "../storage/cloudStorage";
import { getTempPathByJobId } from "../storage/temp-dirs";
import fs from "fs";
import { ensurePathExists } from "../storage/ensurePathExists";
import path from "path";
import {
  addJobDataAndIOEvent,
  ensureJobAndInitStatusRec,
  ensureJobDependencies,
  getJobDataAndIoEvents,
  getJobRec,
  updateJobStatus,
} from "../db/knexConn";
import longStringTruncator from "../utils/longStringTruncator";
import {
  WrapTerminatorAndDataId,
  ZZPipe,
  getPubSubQueueId,
} from "../microworkers/ZZPipe";
import { PipeDef, ZZEnv } from "../microworkers/PipeRegistry";
import { identifyLargeFiles } from "../files/file-ops";
import { z } from "zod";
import Redis from "ioredis";

export type ZZProcessor<
  P,
  O,
  StreamI = never,
  WP extends object = never,
  TProgress = never
> = Parameters<ZZJob<P, O, StreamI, WP, TProgress>["beginProcessing"]>[0];
export type ZZProcessorParams<
  P,
  O,
  StreamI = never,
  WP extends object = never,
  TProgress = never
> = Parameters<ZZProcessor<P, O, StreamI, WP, TProgress>>[0];

export class ZZJob<
  P,
  O,
  StreamI = never,
  WP extends object = never,
  Progress = never
> {
  private readonly bullMQJob: Job<{ jobParams: P }, O | void>;
  public readonly _bullMQToken?: string;
  jobParams: P;
  inputObservable: Observable<StreamI | null>;
  logger: ReturnType<typeof getLogger>;
  // New properties for subscriber tracking
  private inputSubscriberCountObservable: Observable<number>;

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
  nextInput: () => Promise<StreamI | null>;
  emitOutput: (o: O) => Promise<void>;
  signalOutputEnd: () => Promise<void>;

  dedicatedTempWorkingDir: string;
  baseWorkingRelativePath: string;

  storageProvider?: IStorageProvider;
  flowProducer: FlowProducer;
  pipe: ZZPipe<P, O, StreamI, WP, Progress>;
  readonly def: PipeDef<P, O, StreamI, WP, Progress>;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  private inputObservableUntracked: Observable<StreamI | null>;
  public jobId: string;

  constructor(p: {
    bullMQJob: Job<{ jobParams: P }, O | undefined, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    jobParams: P;
    flowProducer: FlowProducer;
    storageProvider?: IStorageProvider;
    pipe: ZZPipe<P, O, StreamI, WP, Progress>;
    workerInstanceParams?: WP;
  }) {
    this.bullMQJob = p.bullMQJob;
    this.jobId = this.bullMQJob.id!;
    this._bullMQToken = p.bullMQToken;
    this.logger = p.logger;

    try {
      this.jobParams = p.pipe.def.jobParams.parse(p.jobParams);
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
      this.workerInstanceParams = (p.pipe.def.workerInstanceParams?.parse(
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
    this.flowProducer = p.flowProducer;
    this.storageProvider = p.storageProvider;
    this.def = p.pipe.def;
    this.zzEnv = p.pipe.zzEnv;
    this.pipe = p.pipe;
    this.baseWorkingRelativePath = path.join(
      this.zzEnv.projectId,
      this.def.name,
      this.jobId!
    );

    const tempWorkingDir = getTempPathByJobId(this.baseWorkingRelativePath);
    this.dedicatedTempWorkingDir = tempWorkingDir;
    const inputPubSubFactory = this.pipe.pubSubFactoryForJob<StreamI>({
      jobId: this.jobId,
      type: "input",
    });
    const outputPubSubFactory = this.pipe.pubSubFactoryForJob<O>({
      jobId: this.jobId,
      type: "output",
    });

    this.inputObservableUntracked = inputPubSubFactory.valueObsrvable.pipe(
      map((x) => (x.terminate ? null : x.data))
    );

    const { trackedObservable, subscriberCountObservable } =
      createTrackedObservable(this.inputObservableUntracked);

    const { nextValue } = createLazyNextValueGenerator(trackedObservable);

    this.nextInput = nextValue;
    this.inputObservable = trackedObservable;
    this.inputSubscriberCountObservable = subscriberCountObservable;

    this.inputSubscriberCountObservable.subscribe((count) => {
      console.log("count", count);
      if (count > 0) {
        setJobReadyForInputsInRedis(this.jobId, true);
      }
    });

    this.signalOutputEnd = async () => {
      await outputPubSubFactory.emitValue({
        terminate: true,
      });
    };

    this.emitOutput = async (o: O) => {
      try {
        o = this.pipe.def.output.parse(o);
      } catch (err) {
        console.error("errornous output: ", o);
        this.logger.error(
          `EmitOutput error: data provided is invalid: ${JSON.stringify(err)}`
        );
        throw err;
      }
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

      const { jobDataId } = await addJobDataAndIOEvent({
        projectId: this.zzEnv.projectId,
        opName: this.def.name,
        jobId: this.jobId,
        dbConn: this.zzEnv.db,
        ioType: "out",
        jobData: o,
      });
      // update progress to prevent job from being stalling
      this.bullMQJob.updateProgress(this._dummyProgressCount++);
      await outputPubSubFactory.emitValue({
        data: o,
        __zz_job_data_id__: jobDataId,
        terminate: false,
      });
    };
  }

  public beginProcessing = async (
    processor: (j: ZZJob<P, O, StreamI, WP, Progress>) => Promise<O | void>
  ): Promise<O | undefined> => {
    const jId = {
      opName: this.def.name,
      jobId: this.jobId,
      projectId: this.zzEnv.projectId,
    };
    const savedResult = await getJobRec<O>({
      ...jId,
      dbConn: this.zzEnv.db,
      jobStatus: "completed",
    });
    const job = this.bullMQJob;
    const logger = this.logger;
    const projectId = this.zzEnv.projectId;

    if (savedResult) {
      const jobData = await getJobDataAndIoEvents<O>({
        ...jId,
        dbConn: this.zzEnv.db,
        ioType: "out",
      });
      this.logger.info(
        `Job already marked as complete; skipping: ${job.id}, ${this.bullMQJob.queueName} ` +
          `${JSON.stringify(this.bullMQJob.data, longStringTruncator)}`,
        +`${JSON.stringify(await job.getChildrenValues(), longStringTruncator)}`
      );

      // return last job data
      return jobData[jobData.length - 1]?.data || undefined;
    } else {
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
        await new Promise<void>((resolve) => {
          this.inputSubscriberCountObservable
            .pipe(takeUntil(this.inputObservableUntracked))
            .subscribe((count) => {
              if (count === 0) {
                resolve();
              }
            });
        });

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

        if (processedR) {
          return processedR;
        }
      } catch (e: any) {
        if (e instanceof WaitingChildrenError) {
          await updateJobStatus({
            projectId,
            opName: this.def.name,
            jobId: job.id!,
            dbConn: this.zzEnv.db,
            jobStatus: "waiting_children",
          });
        } else {
          await updateJobStatus({
            projectId,
            opName: this.def.name,
            jobId: job.id!,
            dbConn: this.zzEnv.db,
            jobStatus: "failed",
          });
        }
        throw e;
      }
    }
  };

  public spawnChildJobsToWaitOn = async <CI, CO>(p: {
    def: PipeDef<P, O>;
    jobId: string;
    jobParams: P;
  }) => {
    const spawnR = await this._spawnJob({
      ...p,
      flowProducerOpts: {
        parent: {
          id: this.jobId,
          queue: this.bullMQJob.queueQualifiedName,
        },
      },
    });

    await ensureJobDependencies({
      projectId: this.zzEnv.projectId,
      parentJobId: this.jobId,
      parentOpName: this.def.name,
      childJobId: p.jobId,
      childOpName: p.def.name,
      dbConn: this.zzEnv.db,
      io_event_id: spawnR.ioEventId,
    });

    return spawnR;
  };

  public spawnJob = async <P, O>(p: {
    def: PipeDef<P, O, any, any, any>;
    jobId: string;
    jobParams: P;
  }) => {
    return await this._spawnJob(p);
  };

  private _spawnJob = async <P, O>({
    def: childJobDef,
    jobId: childJobId,
    jobParams,
    flowProducerOpts,
  }: {
    def: PipeDef<P, O, any, any, any>;
    jobId: string;
    jobParams: P;
    flowProducerOpts?: FlowJob["opts"];
  }) => {
    const tempPipe = new ZZPipe({
      def: childJobDef,
      zzEnv: this.zzEnv,
    });
    await tempPipe.requestJob({
      jobId: childJobId,
      jobParams,
      bullMQJobsOpts: flowProducerOpts,
    });
    const [rec] = await getJobDataAndIoEvents({
      projectId: this.zzEnv.projectId,
      opName: childJobDef.name,
      jobId: childJobId,
      dbConn: this.zzEnv.db,
      ioType: "init-params",
    });

    // await this.flowProducer.add({
    //   name: childJobId,
    //   data: {
    //     jobParams,
    //   },
    //   queueName: `${this.zzEnv.projectId}/${childJobDef.name}`,
    //   opts: {
    //     jobId: childJobId,
    //     ...flowProducerOpts,
    //   },
    // });

    // const rec = await ensureJobAndInitStatusRec({
    //   projectId: this.zzEnv.projectId,
    //   dbConn: this.zzEnv.db,
    //   opName: childJobDef.name,
    //   jobId: childJobId,
    //   jobParams,
    // });

    const jobThat = this;

    let _outFactoriesAndFns: {
      factory: PubSubFactory<WrapTerminatorAndDataId<O>>;
      nextValue: any;
    } | null = null;
    const _getOrCreateOutputPubSubFactory = () => {
      if (!_outFactoriesAndFns) {
        const factory = new PubSubFactory<WrapTerminatorAndDataId<O>>(
          "output",
          {
            projectId: jobThat.zzEnv.projectId,
          },
          jobThat.zzEnv.redisConfig,
          getPubSubQueueId({
            def: childJobDef,
            jobId: childJobId,
          })
        );
        const { nextValue } = createLazyNextValueGenerator(
          factory.valueObsrvable
        );
        _outFactoriesAndFns = {
          factory,
          nextValue,
        };
      }

      return _outFactoriesAndFns;
    };

    return {
      get outputObservable() {
        return _getOrCreateOutputPubSubFactory().factory.valueObsrvable.pipe(
          map((x) => (x.terminate ? null : x.data))
        );
      },
      nextOutput: async () => {
        const r = await _getOrCreateOutputPubSubFactory().nextValue();
        if (r.terminate) {
          return null;
        } else {
          return r.data;
        }
      },
      ...rec,
    };
  };

  public saveToTextFile = async ({
    relativePath,
    data,
  }: {
    relativePath: string;
    data: string;
  }) => {
    await ensurePathExists(this.dedicatedTempWorkingDir);
    fs.writeFileSync(
      path.join(this.dedicatedTempWorkingDir, relativePath),
      data
    );
  };

  getLargeValueCdnUrl = async <T extends object>(key: keyof T, obj: T) => {
    if (!this.storageProvider) {
      throw new Error("storageProvider is not provided");
    }
    if (!this.storageProvider.getPublicUrl) {
      throw new Error("storageProvider.getPublicUrl is not provided");
    }
    const { largeFilesToSave } = identifyLargeFiles(obj);
    const found = largeFilesToSave.find((x) => x.path === key);
    if (!found) {
      console.error("Available keys: ", Object.keys(obj));
      throw new Error(`Cannot find ${String(key)} in largeFilesToSave`);
    } else {
      return getPublicCdnUrl({
        projectId: this.zzEnv.projectId,
        jobId: this.jobId,
        key: String(key),
        storageProvider: this.storageProvider,
      });
    }
  };
}

export async function setJobReadyForInputsInRedis(
  jobId: string,
  isReady: boolean
) {
  try {
    const redis = new Redis();
    await redis.set(`ready_status__${jobId}`, isReady ? "true" : "false");
  } catch (error) {
    console.error("Error setJobReadyForInputsInRedis:", error);
  }
}
