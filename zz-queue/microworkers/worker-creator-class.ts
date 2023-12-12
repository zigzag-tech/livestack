import {
  _upsertAndMergeJobLogByIdAndType,
  ensureJobDependencies,
  getJobLogByIdAndType,
} from "../db/knexConn";
import fs from "fs";
import _ from "lodash";
import {
  Worker,
  Job,
  FlowProducer,
  WaitingChildrenError,
  FlowJob,
  WorkerOptions,
  Processor,
} from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { getMicroworkerQueueByName, longStringTruncator } from "./queues";
import { Knex } from "knex";
import { TEMP_DIR, getTempPathByJobId } from "../storage/temp-dirs";
import { ensurePathExists } from "../storage/ensurePathExists";
import path from "path";
import { isBinaryLikeObject } from "../utils/isBinaryLikeObject";
import {
  IStorageProvider,
  getPublicCdnUrl,
  saveLargeFilesToStorage,
} from "../storage/cloudStorage";
import Redis, { RedisOptions } from "ioredis";
import {
  PubSubFactory,
  sequentialInputFactory,
  sequentialOutputFactory,
} from "../realtime/mq-pub-sub";
import { Observable } from "rxjs";

const OBJ_REF_VALUE = `__zz_obj_ref__`;
const LARGE_VALUE_THRESHOLD = 1024 * 10;
const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs<I, O> = ReturnType<
  typeof getMicroworkerQueueByName<I, O, any>
>;

export type ZZProcessor<I, O> = ZZWorker<I, O>["processor"];
export type ZZProcessorParams<I, O> = Parameters<ZZProcessor<I, O>>[0];
export type ZZProcessorReturn<I, O> = ReturnType<ZZProcessor<I, O>>;

export abstract class ZZWorker<I, O> implements IWorkerUtilFuncs<I, O> {
  protected queueName: string;
  protected readonly db: Knex;
  protected readonly workerOptions: WorkerOptions;
  protected readonly storageProvider?: IStorageProvider;

  public readonly bullMQWorker: Worker<
    {
      firstInput: I;
    },
    O
  >;
  protected color?: string;

  public readonly addJob: IWorkerUtilFuncs<I, O>["addJob"];
  public readonly getJob: IWorkerUtilFuncs<I, O>["getJob"];
  public readonly cancelJob: IWorkerUtilFuncs<I, O>["cancelJob"];
  public readonly pingAlive: IWorkerUtilFuncs<I, O>["pingAlive"];
  public readonly getJobData: IWorkerUtilFuncs<I, O>["getJobData"];
  public readonly enqueueJobAndGetResult: IWorkerUtilFuncs<
    I,
    O
  >["enqueueJobAndGetResult"];
  public readonly _rawQueue: IWorkerUtilFuncs<I, O>["_rawQueue"];

  constructor({
    queueName,
    projectId,
    db,
    redisConfig,
    color,
    storageProvider,
    concurrency = 3,
  }: {
    queueName: string;
    projectId: string;
    db: Knex;
    color?: string;
    redisConfig: RedisOptions;
    storageProvider?: IStorageProvider;
    concurrency?: number;
  }) {
    this.queueName = queueName;
    this.db = db;
    this.workerOptions = {
      autorun: false,
      concurrency,
      connection: redisConfig,
    };
    this.storageProvider = storageProvider;
    this.color = color;

    const queueFuncs = getMicroworkerQueueByName<I, O, any>({
      queueName: this.queueName,
      workerOptions: this.workerOptions,
      db: this.db,
      projectId,
    });

    this.addJob = queueFuncs.addJob;
    this.cancelJob = queueFuncs.cancelJob;
    this.pingAlive = queueFuncs.pingAlive;
    this.enqueueJobAndGetResult = queueFuncs.enqueueJobAndGetResult;
    this._rawQueue = queueFuncs._rawQueue;
    this.getJob = queueFuncs.getJob;
    this.getJobData = queueFuncs.getJobData;

    const logger = getLogger(`wkr:${this.queueName}`, this.color);
    const mergedWorkerOptions = _.merge({}, this.workerOptions);
    const flowProducer = new FlowProducer(mergedWorkerOptions);

    this.bullMQWorker = new Worker(
      this.queueName,
      async (job, token) => {
        const savedResult = await getJobLogByIdAndType({
          jobType: this.queueName,
          jobId: job.id!,
          projectId,
          dbConn: this.db,
          jobStatus: "completed",
        });
        if (savedResult) {
          logger.info(
            `Skipping job with ID: ${job.id}, ${job.queueName} ` +
              `${JSON.stringify(job.data, longStringTruncator)}`,
            +`${JSON.stringify(
              await job.getChildrenValues(),
              longStringTruncator
            )}`
          );
          return savedResult.job_data;
        } else {
          logger.info(
            `Picked up job with ID: ${job.id}, ${job.queueName} ` +
              `${JSON.stringify(job.data, longStringTruncator)}`,
            +`${JSON.stringify(
              await job.getChildrenValues(),
              longStringTruncator
            )}`
          );

          const ensureLocalSourceFileExists = async (filePath: string) => {
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
          };
          const getLargeValueCdnUrl = <T extends object>(
            key: keyof T,
            obj: T
          ) => {
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
                projectId,
                jobId: job.id!,
                key: String(key),
                storageProvider: this.storageProvider,
              });
            }
          };

          const workingDir = getTempPathByJobId(job.id!);

          const saveToTextFile = async ({
            relativePath,
            data,
          }: {
            relativePath: string;
            data: string;
          }) => {
            await ensurePathExists(workingDir);
            fs.writeFileSync(path.join(workingDir, relativePath), data);
          };

          const update = async ({
            incrementalData,
          }: {
            incrementalData?: any;
          }) => {
            let jobData: any;

            if (this.storageProvider) {
              const { newObj, largeFilesToSave } = identifyLargeFiles(
                incrementalData,
                `${projectId}/jobs/${job.id!}/large-values`
              );
              await saveLargeFilesToStorage(
                largeFilesToSave,
                this.storageProvider
              );
              jobData = newObj;
            } else {
              jobData = incrementalData;
            }
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: this.queueName,
              jobId: job.id!,
              jobData,
              dbConn: this.db,
            });
          };
          const pubSubFactory = new PubSubFactory<I, O>(
            {
              projectId,
            },
            redisConfig,
            queueName + "::" + job.id!
          );

          const spawnJob = async <CI, CO>(
            newJob_: FlowJob & {
              data: CI;
            }
          ) => {
            newJob_.data = {
              firstInput: newJob_.data,
            };

            newJob_.opts = {
              ...newJob_.opts,
              jobId: newJob_.name,
            };

            const pubsubForChild = new PubSubFactory<CI, CO>(
              {
                projectId,
              },
              redisConfig,
              newJob_.queueName + "::" + newJob_.name
            );
            await flowProducer.add(newJob_);
            return {
              subToOutput: pubsubForChild.subForJobOutput.bind(pubsubForChild),
            };
          };

          const spawnChildJobsToWaitOn = async <CI, CO>(
            childJob_: FlowJob & {
              data: CI;
            }
          ) => {
            childJob_.opts = {
              ...childJob_.opts,
              parent: {
                id: job.id!,
                queue: job.queueQualifiedName,
              },
            };

            return spawnJob<CI, CO>(childJob_);
          };

          try {
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: this.queueName,
              jobId: job.id!,
              dbConn: this.db,
              jobStatus: "active",
            });
            if (job.opts.parent?.id) {
              await ensureJobDependencies({
                parentJobId: job.opts.parent.id,
                childJobId: job.id!,
                dbConn: this.db,
                projectId,
              });
            }

            const aliveLoop = async (retVal: O) => {
              const redis = new Redis();

              let lastTimeJobAlive = Date.now();
              while (Date.now() - lastTimeJobAlive < JOB_ALIVE_TIMEOUT) {
                lastTimeJobAlive = parseInt(
                  (await redis.get(`last-time-job-alive-${job.id}`)) || "0"
                );
                await sleep(1000 * 60);
              }
              logger.info(
                `Job with ${job.id} id expired. Marking as complete.`
              );
              await job.moveToCompleted(retVal, token!);
              return retVal;
            };

            const { nextInput, inputObservable } = sequentialInputFactory<I>({
              pubSubFactory: pubSubFactory,
            });

            const { emitOutput } = sequentialOutputFactory<O>({
              pubSubFactory: pubSubFactory,
            });

            const processedR = await this.processor({
              job,
              token,
              logger,
              inputObservable,
              aliveLoop,
              workingDirToBeUploadedToCloudStorage: workingDir,
              ensureLocalSourceFileExists,
              saveToTextFile,
              spawnChildJobsToWaitOn,
              spawnJob,
              getLargeValueCdnUrl,
              update,
              firstInput: job.data.firstInput,
              nextInput,
              emitOutput,
            });
            // await job.updateProgress(processedR as object);
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: this.queueName,
              jobId: job.id!,
              dbConn: this.db,
              jobStatus: "completed",
            });
            return processedR;
          } catch (e: any) {
            if (e instanceof WaitingChildrenError) {
              await _upsertAndMergeJobLogByIdAndType({
                projectId,
                jobType: this.queueName,
                jobId: job.id!,
                dbConn: this.db,
                jobStatus: "waiting_children",
              });
            } else {
              await _upsertAndMergeJobLogByIdAndType({
                projectId,
                jobType: this.queueName,
                jobId: job.id!,
                dbConn: this.db,
                jobStatus: "failed",
              });
              throw e;
            }
          }
        }
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

    this.bullMQWorker.on("completed", async (job: Job, result: O) => {
      logger.info(`JOB COMPLETED: ${job.id}`);
    });

    this.bullMQWorker.run();

    logger.info(`${this.queueName} worker started.`);
  }

  protected abstract processor(p: {
    job: Job<{ firstInput: I }, O>;
    token?: string;
    firstInput: I;
    inputObservable: Observable<I>;
    logger: ReturnType<typeof getLogger>;
    aliveLoop: (retVal: O) => Promise<O>;
    nextInput: () => Promise<I>;
    emitOutput: (o: O) => Promise<void>;
    spawnChildJobsToWaitOn: <CI, CO>(
      job: FlowJob & {
        data: CI;
      }
    ) => Promise<{ subToOutput: PubSubFactory<CI, CO>["subForJobOutput"] }>;
    spawnJob: <CI, CO>(
      job: FlowJob & {
        data: CI;
      }
    ) => Promise<{ subToOutput: PubSubFactory<CI, CO>["subForJobOutput"] }>;
    workingDirToBeUploadedToCloudStorage: string;
    update: (p: {
      incrementalData?: Partial<I>;
      progressPercentage?: number;
    }) => Promise<void>;
    saveToTextFile: (p: {
      relativePath: string;
      data: string;
    }) => Promise<void>;
    ensureLocalSourceFileExists: (filePath: string) => Promise<void>;
    getLargeValueCdnUrl: <T extends object>(key: keyof T, obj: T) => string;
  }): ReturnType<Processor<I, O>>;
}

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

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
