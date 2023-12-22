import { Job, FlowJob, FlowProducer, WaitingChildrenError } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { PubSubFactory, sequentialFactory } from "../realtime/mq-pub-sub";
import { Observable, Subject, map, takeUntil, takeWhile, tap } from "rxjs";
import {
  IStorageProvider,
  getPublicCdnUrl,
  saveLargeFilesToStorage,
} from "../storage/cloudStorage";
import { isBinaryLikeObject } from "../utils/isBinaryLikeObject";
import { TEMP_DIR, getTempPathByJobId } from "../storage/temp-dirs";
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
import { WrapTerminatorAndDataId, ZZPipe } from "./ZZPipe";
import { PipeDef, ZZEnv } from "./PipeRegistry";
import { z } from "zod";

export type ZZProcessor<P, O, StreamI = never> = Parameters<
  ZZJob<P, O, StreamI>["beginProcessing"]
>[0];
export type ZZProcessorParams<P, O, StreamI = never> = Parameters<
  ZZProcessor<P, O, StreamI>
>[0];

export class ZZJob<P, O, StreamI = never> {
  private readonly bullMQJob: Job<{ params: P }, O | void>;
  public readonly _bullMQToken?: string;
  params: P;
  inputObservable: Observable<StreamI | null>;
  logger: ReturnType<typeof getLogger>;
  // New properties for subscriber tracking
  private inputSubscriberCount = 0;
  private inputSubscriberCountChanged = new Subject<number>();

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
  pipe: ZZPipe<P, O, StreamI>;
  readonly def: PipeDef<P, O, StreamI>;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;

  constructor(p: {
    bullMQJob: Job<{ params: P }, O | undefined, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    params: P;
    flowProducer: FlowProducer;
    storageProvider?: IStorageProvider;
    pipe: ZZPipe<P, O, StreamI>;
  }) {
    this.bullMQJob = p.bullMQJob;
    this._bullMQToken = p.bullMQToken;
    this.params = p.params;
    this.logger = p.logger;
    this.flowProducer = p.flowProducer;
    this.storageProvider = p.storageProvider;
    this.def = p.pipe.def;
    this.zzEnv = p.pipe.zzEnv;
    this.pipe = p.pipe;

    const workingDir = getTempPathByJobId(this.bullMQJob.id!);
    this.workingDirToBeUploadedToCloudStorage = workingDir;
    // console.log("pubSubFactoryForJobj", this.bullMQJob.id!);
    const pubSubFactory = this.pipe.pubSubFactoryForJob(this.bullMQJob.id!);

    // const pubSubFactory = new PubSubFactory<
    //   StreamI,
    //   {
    //     o: O;
    //     __zz_job_data_id__: string;
    //   }
    // >(
    //   {
    //     projectId: this.zzEnv.projectId,
    //   },
    //   this.zzEnv.redisConfig,
    //   this.def.name + "::" + this.bullMQJob.id!
    // );

    const { nextValue: nextInput, valueObsrvable: inputObservable } =
      sequentialFactory<WrapTerminatorAndDataId<StreamI>>({
        pubSubFactory,
      });

    this.nextInput = async () => {
      const r = await nextInput();
      if (r.terminate) {
        return null;
      } else {
        return r.data;
      }
    };
    this.inputObservable = inputObservable
      .pipe(map((r) => (r.terminate ? null : r.data)))
      .pipe(
        tap({
          subscribe: () => {
            this.inputSubscriberCount++;
            this.inputSubscriberCountChanged.next(this.inputSubscriberCount);
          },
          unsubscribe: () => {
            this.inputSubscriberCount--;
            this.inputSubscriberCountChanged.next(this.inputSubscriberCount);
          },
        })
      );

    const { emit: emitOutput } = sequentialFactory<WrapTerminatorAndDataId<O>>({
      pubSubFactory,
    });

    this.emitOutput = async (o: O) => {
      if (this.storageProvider) {
        const { largeFilesToSave } = identifyLargeFiles(o);
        await saveLargeFilesToStorage(largeFilesToSave, this.storageProvider);
      }
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
      await emitOutput({
        data: o,
        __zz_job_data_id__: jobDataId,
        terminate: false,
      });
    };
  }

  public async beginProcessing(
    processor: (j: ZZJob<P, O, StreamI>) => Promise<O | void>
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

        // await job.updateProgress(processedR as object);
        await updateJobStatus({
          ...jId,
          dbConn: this.zzEnv.db,
          jobStatus: "completed",
        });

        // wait as long as there are still subscribers
        await new Promise<void>((resolve) => {
          const sub = this.inputSubscriberCountChanged
            .pipe(takeUntil(this.inputObservable))
            .subscribe((count) => {
              if (count === 0) {
                sub.unsubscribe();
                resolve();
              }
            });
        });

        if (processedR) {
          await this.emitOutput(processedR);
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

  public async spawnChildJobsToWaitOn<CI, CO>(
    childJob_: FlowJob & {
      initParams: CI;
    }
  ) {
    childJob_.opts = {
      ...childJob_.opts,
      parent: {
        id: this.bullMQJob.id!,
        queue: this.bullMQJob.queueQualifiedName,
      },
    };

    const spawnR = await this.spawnJob<CI, CO>(childJob_);

    await ensureJobDependencies({
      projectId: this.zzEnv.projectId,
      parentJobId: this.bullMQJob.id!,
      parentOpName: this.def.name,
      childJobId: childJob_.opts.jobId!,
      childOpName: childJob_.name,
      dbConn: this.zzEnv.db,
      io_event_id: spawnR.ioEventId,
    });

    return spawnR;
  }

  public async spawnJob<I, O>(
    newJob_: FlowJob & {
      initParams: I;
    }
  ) {
    // newJob_.data = {
    //   params: newJob_.data as I,
    // };

    newJob_.opts = {
      ...newJob_.opts,
      jobId: newJob_.name,
    };
    // console.log("queueIda", newJob_.queueName + "::" + newJob_.name);
    const pubsubForChild = new PubSubFactory<O>(
      {
        projectId: this.zzEnv.projectId,
      },
      this.zzEnv.redisConfig,
      newJob_.queueName + "::" + newJob_.name
    );
    await this.flowProducer.add(newJob_);

    const rec = await addJobRec({
      projectId: this.zzEnv.projectId,
      opName: this.def.name,
      jobId: this.bullMQJob.id!,
      dbConn: this.zzEnv.db,
      initParams: newJob_.initParams,
    });
    return {
      subToOutput: (processor: (message: O) => void) => {
        pubsubForChild.subForJob({
          type: "output",
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

  // public async update({ incrementalData }: { incrementalData?: any }) {
  //   let jobData: any;

  //   if (this.storageProvider) {
  //     const { newObj, largeFilesToSave } = identifyLargeFiles(
  //       incrementalData,
  //       `${this.zzEnv.projectId}/jobs/${this.bullMQJob.id!}/large-values`
  //     );
  //     await saveLargeFilesToStorage(largeFilesToSave, this.storageProvider);
  //     jobData = newObj;
  //   } else {
  //     jobData = incrementalData;
  //   }
  //   await _upsertAndMergeJobLogByIdAndType({
  //     projectId: this.zzEnv.projectId,
  //     jobType: this.def.name,
  //     jobId: this.bullMQJob.id!,
  //     jobData,
  //     dbConn: this.zzEnv.db,
  //   });
  // }

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

  private async ensureLocalSourceFileExists(filePath: string) {
    try {
      fs.accessSync(filePath);
    } catch (error) {
      if (this.storageProvider) {
        ensurePathExists(filePath);
        const gcsFileName = filePath
          .split(`${TEMP_DIR}/`)[1]
          .replace(/_/g, "/");
        await this.storageProvider.downloadFromStorage({
          filePath: gcsFileName,
          destination: filePath,
        });
      }
    }
  }
}

const OBJ_REF_VALUE = `__zz_obj_ref__`;
const LARGE_VALUE_THRESHOLD = 1024 * 10;

export const identifyLargeFiles = (
  obj: any,
  path = ""
): { newObj: any; largeFilesToSave: { path: string; value: any }[] } => {
  if (obj === null || typeof obj !== "object") {
    return { newObj: obj, largeFilesToSave: [] };
  }
  const newObj: any = Array.isArray(obj) ? [] : {};
  const largeFilesToSave: { path: string; value: any }[] = [];

  for (const [key, value] of Object.entries(obj)) {
    const currentPath = path ? `${path}/${key}` : key;
    if (typeof value === "string" && value.length > LARGE_VALUE_THRESHOLD) {
      largeFilesToSave.push({ path: currentPath, value });
      newObj[key] = OBJ_REF_VALUE;
    } else if (isBinaryLikeObject(value)) {
      largeFilesToSave.push({ path: currentPath, value });
      newObj[key] = OBJ_REF_VALUE;
    } else if (typeof value === "object") {
      const result = identifyLargeFiles(value, currentPath);
      newObj[key] = result.newObj;
      largeFilesToSave.push(...result.largeFilesToSave);
    } else {
      newObj[key] = value;
    }
  }
  return { newObj, largeFilesToSave };
};
