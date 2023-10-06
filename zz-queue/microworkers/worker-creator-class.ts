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
import { IStorageProvider } from "../storage/cloudStorage";
import Redis from "ioredis";

const OBJ_REF_VALUE = `__zz_obj_ref__`;
const LARGE_VALUE_THRESHOLD = 1024 * 10;
const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;
type IWorkerUtilFuncs = ReturnType<typeof getMicroworkerQueueByName>;

export abstract class ZZWorker<D, R> implements IWorkerUtilFuncs {
  protected queueName: string;
  protected readonly db: Knex;
  protected readonly workerOptions: WorkerOptions;
  protected readonly storageProvider?: IStorageProvider;
  public readonly bullMQWorker: Worker<D, R>;
  protected color?: string;

  public readonly addJob: IWorkerUtilFuncs["addJob"];
  public readonly getJob: IWorkerUtilFuncs["getJob"];
  public readonly cancelJob: IWorkerUtilFuncs["cancelJob"];
  public readonly pingAlive: IWorkerUtilFuncs["pingAlive"];
  public readonly enqueueJobAndGetResult: IWorkerUtilFuncs["enqueueJobAndGetResult"];
  public readonly _rawQueue: IWorkerUtilFuncs["_rawQueue"];

  constructor({
    queueName,
    projectId,
    db,
    workerOptions,
    color,
    storageProvider,
  }: {
    queueName: string;
    projectId: string;
    db: Knex;
    color?: string;
    workerOptions: WorkerOptions;
    storageProvider?: IStorageProvider;
  }) {
    this.queueName = queueName;
    this.db = db;
    this.workerOptions = { autorun: false, ...workerOptions };
    this.storageProvider = storageProvider;
    this.color = color;

    const queueFuncs = getMicroworkerQueueByName({
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

          const spawnChildJobsToWaitOn = async (job_: FlowJob | FlowJob[]) => {
            if (!Array.isArray(job_)) {
              await _spawn(job_);
            } else {
              for (const j of job_) {
                await _spawn(j);
              }
            }
          };

          const _spawn = async (childJob_: FlowJob) => {
            childJob_.opts = {
              ...childJob_.opts,
              parent: {
                id: job.id!,
                queue: job.queueQualifiedName,
              },
            };

            const childJ = await flowProducer.add(childJob_);
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

            const aliveLoop = async (retVal: R) => {
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

            const processedR = await this.processor({
              job,
              token,
              logger,
              aliveLoop,
              workingDirToBeUploadedToCloudStorage: workingDir,
              ensureLocalSourceFileExists,
              saveToTextFile,
              spawnChildJobsToWaitOn,
              getLargeValueCdnUrl,
              update,
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

    this.bullMQWorker.on("completed", async (job: Job, result: R) => {
      logger.info(`JOB COMPLETED: ${job.id}`);
    });

    this.bullMQWorker.run();

    logger.info(`${this.queueName} worker started.`);
  }

  protected abstract processor(p: {
    job: Parameters<Processor<D, R>>[0];
    token: Parameters<Processor<D, R>>[1];
    logger: ReturnType<typeof getLogger>;
    aliveLoop: (retVal: R) => Promise<R>;
    spawnChildJobsToWaitOn: (job: FlowJob | FlowJob[]) => Promise<void>;
    workingDirToBeUploadedToCloudStorage: string;
    update: (p: {
      incrementalData?: Partial<D>;
      progressPercentage?: number;
    }) => Promise<void>;
    saveToTextFile: (p: {
      relativePath: string;
      data: string;
    }) => Promise<void>;
    ensureLocalSourceFileExists: (filePath: string) => Promise<void>;
    getLargeValueCdnUrl: <T extends object>(key: keyof T, obj: T) => string;
  }): ReturnType<Processor<D, R>>;
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

export const saveLargeFilesToStorage = async (
  largeFilesToSave: { path: string; value: any }[],
  storageProvider: IStorageProvider
): Promise<void> => {
  for (const { path, value } of largeFilesToSave) {
    await storageProvider.putToStorage(path, value);
  }
};

export function getPublicCdnUrl({
  projectId,
  jobId,
  key,
  storageProvider,
}: {
  projectId: string;
  jobId: string;
  key: string;
  storageProvider: IStorageProvider;
}) {
  if (!storageProvider.getPublicUrl) {
    throw new Error("storageProvider.getPublicUrl is not provided");
  }
  const fullPath = `/${projectId}/jobs/${jobId}/large-values/${key}`;
  return storageProvider.getPublicUrl(fullPath);
}
export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
