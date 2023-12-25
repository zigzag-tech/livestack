import { Job, FlowJob, FlowProducer, WaitingChildrenError } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { PubSubFactory } from "../realtime/mq-pub-sub";
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
  addJobRec,
  ensureJobDependencies,
  getJobData,
  getJobRec,
  updateJobStatus,
} from "../db/knexConn";
import longStringTruncator from "../utils/longStringTruncator";
import { ZZPipe } from "../microworkers/ZZPipe";
import { PipeDef, ZZEnv } from "../microworkers/PipeRegistry";
import { identifyLargeFiles } from "../files/file-ops";
import { z } from "zod";
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
  WP extends object = {},
  Progress = never
> {
  private readonly bullMQJob: Job<{ initParams: P }, O | void>;
  public readonly _bullMQToken?: string;
  initParams: P;
  inputObservable: Observable<StreamI | null>;
  logger: ReturnType<typeof getLogger>;
  // New properties for subscriber tracking
  private inputSubscriberCountChanged = new Subject<number>();
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
  //     `Job with ${this.bullMQJob.id} id expired. Marking as complete.`
  //   );
  //   await this.bullMQJob.moveToCompleted(retVal, this._bullMQToken!);
  //   return retVal;
  // }
  nextInput: () => Promise<StreamI | null>;
  emitOutput: (o: O) => Promise<void>;

  workingDirToBeUploadedToCloudStorage: string;

  storageProvider?: IStorageProvider;
  flowProducer: FlowProducer;
  pipe: ZZPipe<P, O, StreamI, WP, Progress>;
  readonly def: PipeDef<P, O, StreamI, WP, Progress>;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP | {} = {};
  private inputObservableUntracked: Observable<StreamI | null>;

  constructor(p: {
    bullMQJob: Job<{ initParams: P }, O | undefined, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    initParams: P;
    flowProducer: FlowProducer;
    storageProvider?: IStorageProvider;
    pipe: ZZPipe<P, O, StreamI, WP, Progress>;
    workerInstanceParams?: WP;
  }) {
    this.bullMQJob = p.bullMQJob;
    this._bullMQToken = p.bullMQToken;
    this.initParams = p.pipe.def.jobParams.parse(p.initParams);

    this.logger = p.logger;
    try {
      this.workerInstanceParams =
        p.pipe.def.workerInstanceParams?.parse(p.workerInstanceParams) ||
        ({} as WP);
    } catch (err) {
      if (err instanceof z.ZodError) {
        this.logger.error(
          `workerInstanceParams error: data provided is invalid: ${err.message}`
        );
        throw err;
      }
    }
    this.flowProducer = p.flowProducer;
    this.storageProvider = p.storageProvider;
    this.def = p.pipe.def;
    this.zzEnv = p.pipe.zzEnv;
    this.pipe = p.pipe;

    const workingDir = getTempPathByJobId(this.bullMQJob.id!);
    this.workingDirToBeUploadedToCloudStorage = workingDir;
    // console.log("pubSubFactoryForJobj", this.bullMQJob.id!);
    const inputPubSubFactory = this.pipe.pubSubFactoryForJob<StreamI>({
      jobId: this.bullMQJob.id!,
      type: "input",
    });
    const outputPubSubFactory = this.pipe.pubSubFactoryForJob<O>({
      jobId: this.bullMQJob.id!,
      type: "output",
    });

    this.nextInput = async () => {
      const r = await inputPubSubFactory.nextValue();
      if (r.terminate) {
        return null;
      } else {
        return r.data;
      }
    };

    this.inputObservableUntracked = inputPubSubFactory.valueObsrvable.pipe(
      map((x) => (x.terminate ? null : x.data))
    );

    const { trackedObservable, subscriberCountObservable } =
      createTrackedObservable(this.inputObservableUntracked);

    this.inputObservable = trackedObservable;
    this.inputSubscriberCountObservable = subscriberCountObservable;

    this.emitOutput = async (o: O) => {
      try {
        o = this.pipe.def.output.parse(o);
      } catch (err) {
        if (err instanceof z.ZodError) {
          this.logger.error(
            `EmitOutput error: data provided is invalid: ${err.message}`
          );
          throw err;
        }
      }

      if (this.storageProvider) {
        let { largeFilesToSave, newObj } = identifyLargeFiles(o);
        o = newObj;
        await saveLargeFilesToStorage(largeFilesToSave, this.storageProvider);
      }

      this.logger.info(
        `Emitting output: ${this.bullMQJob.id}, ${this.bullMQJob.queueName} ` +
          `${JSON.stringify(this.bullMQJob.data, longStringTruncator)}`,
        +`${JSON.stringify(
          await this.bullMQJob.getChildrenValues(),
          longStringTruncator
        )}` +
          JSON.stringify(
            {
              projectId: this.zzEnv.projectId,
              opName: this.def.name,
              jobId: this.bullMQJob.id!,
              dbConn: this.zzEnv.db,
              ioType: "out",
              jobData: o,
            },
            longStringTruncator
          )
      );

      const { jobDataId } = await addJobDataAndIOEvent({
        projectId: this.zzEnv.projectId,
        opName: this.def.name,
        jobId: this.bullMQJob.id!,
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

  public async beginProcessing(
    processor: (j: ZZJob<P, O, StreamI, WP, Progress>) => Promise<O | void>
  ): Promise<O | undefined> {
    const jId = {
      opName: this.def.name,
      jobId: this.bullMQJob.id!,
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
      const jobData = await getJobData<O>({
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
      return jobData[jobData.length - 1] || undefined;
    } else {
      logger.info(
        `Picked up job with ID: ${job.id}, ${job.queueName} ` +
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
  }

  public async spawnChildJobsToWaitOn<CI, CO>(p: {
    def: PipeDef<P, O>;
    jobId: string;
    initParams: P;
  }) {
    const spawnR = await this._spawnJob({
      ...p,
      flowProducerOpts: {
        parent: {
          id: this.bullMQJob.id!,
          queue: this.bullMQJob.queueQualifiedName,
        },
      },
    });

    await ensureJobDependencies({
      projectId: this.zzEnv.projectId,
      parentJobId: this.bullMQJob.id!,
      parentOpName: this.def.name,
      childJobId: p.jobId,
      childOpName: p.def.name,
      dbConn: this.zzEnv.db,
      io_event_id: spawnR.ioEventId,
    });

    return spawnR;
  }

  public async spawnJob<P, O>(p: {
    def: PipeDef<P, O>;
    jobId: string;
    initParams: P;
  }) {
    return await this._spawnJob(p);
  }

  private async _spawnJob<P, O>({
    def,
    jobId,
    initParams,
    flowProducerOpts,
  }: {
    def: PipeDef<P, O>;
    jobId: string;
    initParams: P;
    flowProducerOpts?: FlowJob["opts"];
  }) {
    const pubsubForChild = new PubSubFactory<O>(
      "output",
      {
        projectId: this.zzEnv.projectId,
      },
      this.zzEnv.redisConfig,
      def.name + "::" + jobId
    );

    await this.flowProducer.add({
      name: jobId,
      data: {
        initParams,
      },
      queueName: def.name,
      opts: {
        jobId: jobId,
        ...flowProducerOpts,
      },
    });

    const rec = await addJobRec({
      projectId: this.zzEnv.projectId,
      opName: this.def.name,
      jobId: this.bullMQJob.id!,
      dbConn: this.zzEnv.db,
      initParams,
    });

    return {
      subToOutput: (processor: (message: O) => void) => {
        pubsubForChild.subForJob({
          processor,
        });
      },
      ...rec,
    };
  }

  public async saveToTextFile({
    relativePath,
    data,
  }: {
    relativePath: string;
    data: string;
  }) {
    await ensurePathExists(this.workingDirToBeUploadedToCloudStorage);
    fs.writeFileSync(
      path.join(this.workingDirToBeUploadedToCloudStorage, relativePath),
      data
    );
  }

  private getLargeValueCdnUrl<T extends object>(key: keyof T, obj: T) {
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
        jobId: this.bullMQJob.id!,
        key: String(key),
        storageProvider: this.storageProvider,
      });
    }
  }
}

function createTrackedObservable<T>(observable: Observable<T>) {
  let subscriberCount = 0;
  const subscriberCountSubject = new BehaviorSubject<number>(subscriberCount);

  const trackedObservable = new Observable<T>((subscriber: Subscriber<T>) => {
    // Increment subscriber count
    subscriberCount++;
    subscriberCountSubject.next(subscriberCount);
    // console.log(`Subscribers: ${subscriberCount}`);

    // Subscribe to the original observable
    const subscription: Subscription = observable.subscribe({
      next: (value: T) => subscriber.next(value),
      error: (err: any) => subscriber.error(err),
      complete: () => subscriber.complete(),
    });

    // Return the teardown logic
    return () => {
      // Decrement subscriber count
      subscriberCount--;
      subscriberCountSubject.next(subscriberCount);
      // console.log(`Subscribers: ${subscriberCount}`);

      // Unsubscribe from the original observable
      subscription.unsubscribe();
    };
  });

  const subscriberCountObservable = subscriberCountSubject.asObservable();

  return { trackedObservable, subscriberCountObservable };
}
