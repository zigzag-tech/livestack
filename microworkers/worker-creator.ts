import { _upsertAndMergeJobLogByIdAndType } from "../db/knexConn";
import fs from "fs";
import _ from "lodash";
import {
  Worker,
  Job,
  Processor,
  FlowProducer,
  WaitingChildrenError,
  FlowJob,
} from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { getMicroworkerQueueByName, longStringTruncator } from "./queues";
import { GenericRecordType, QueueName } from "./workerCommon";
import { getJobLogByIdAndType } from "../db/knexConn";
import { WorkerOptions } from "bullmq";
import { Knex } from "knex";
import { TEMP_DIR, getTempPathByJobId } from "../storage/temp-dirs";
import { ensurePathExists } from "../storage/ensurePathExists";
import { getStorageBucket, putToStorage } from "../storage/cloudStorage";
import path from "path";
import { isBinaryLikeObject } from "../utils/isBinaryLikeObject";
const OBJ_REF_VALUE = `__zz_obj_ref__`;
const LARGE_VALUE_THRESHOLD = 1024 * 10;
export function createWorkerMainFunction<D, R, T extends GenericRecordType>({
  queueName,
  queueNamesDef,
  projectId,
  processor,
  db,
  workerOptions,
  color,
  storageBucketName,
}: {
  queueName: QueueName<T>;
  queueNamesDef: T;
  projectId: string;
  db: Knex;
  color?: string;
  workerOptions: WorkerOptions;
  storageBucketName?: string;
  processor: (p: {
    job: ArgumentTypes<Processor<D, R>>[0];
    token: ArgumentTypes<Processor<D, R>>[1];
    logger: ReturnType<typeof getLogger>;
    spawnChildJobsToWaitOn: (job: FlowJob | FlowJob[]) => Promise<void>;
    workingDirToBeUploadedToCloudStorage: string;
    update: (p: {
      incrementalData?: any;
      progressPercentage?: number;
    }) => Promise<void>;
    saveToTextFile: (p: {
      relativePath: string;
      data: string;
    }) => Promise<void>;
    ensureLocalSourceFileExists: (filePath: string) => Promise<void>;
  }) => ReturnType<Processor<D, R>>;
}) {
  const mainFn = (args?: Partial<WorkerOptions>) => {
    const queueFuncs = getMicroworkerQueueByName({
      queueNamesDef,
      queueName,
      workerOptions,
      db,
      projectId,
    });
    const logger = getLogger(`wkr:${queueName}`, color);

    const mergedWorkerOptions = _.merge({}, workerOptions, args);

    const flowProducer = new FlowProducer(mergedWorkerOptions);

    const worker = new Worker(
      queueName,
      async (job, token) => {
        const savedResult = await getJobLogByIdAndType({
          jobType: queueName,
          jobId: job.id!,
          projectId,
          dbConn: db,
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
              if (storageBucketName) {
                const storageBucket = getStorageBucket(storageBucketName);
                ensurePathExists(filePath);
                const gcsFileName = filePath
                  .split(`${TEMP_DIR}/`)[1]
                  .replace(/_/g, "/");
                await storageBucket
                  .file(gcsFileName)
                  .download({ destination: filePath });
              }
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

          const convertLargeValueToRefAndSaveToCloud = async ({
            obj,
            path = "",
          }: {
            obj: any;
            path?: string;
          }): Promise<any> => {
            if (obj === null || typeof obj !== "object") {
              return obj;
            }
            const newObj = { ...obj };

            for (const [key, value] of Object.entries(newObj)) {
              const currentPath = path ? `${path}/${key}` : key;
              if (
                typeof value === "string" &&
                value.length > LARGE_VALUE_THRESHOLD
              ) {
                if (!storageBucketName) {
                  throw new Error(
                    `storageBucketName is not defined and the path ${key} is too big to store in database.`
                  );
                }
                // Save large string to storage and replace with reference hash
                await putToStorage(
                  storageBucketName,
                  currentPath,
                  Buffer.from(value)
                );
                newObj[key] = `__zz_obj_ref__`;
              } else if (isBinaryLikeObject(value)) {
                // Save buffer/binary to storage and replace with reference hash
                await putToStorage(
                  storageBucketName!,
                  currentPath,
                  value as Buffer | string | File | Blob | ArrayBuffer
                );
                newObj[key] = OBJ_REF_VALUE;
              } else if (typeof value === "object") {
                // Recursively process nested objects
                newObj[key] = await convertLargeValueToRefAndSaveToCloud({
                  obj: value,
                  path: currentPath,
                });
              }
            }
            return newObj;
          };

          const update = async ({
            incrementalData,
          }: {
            incrementalData?: any;
          }) => {
            const processedData = await convertLargeValueToRefAndSaveToCloud({
              obj: incrementalData,
              path: `${projectId}/jobs/${job.id!}/large-values/`,
            });
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: queueName,
              jobId: job.id!,
              jobData: processedData,
              dbConn: db,
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

          const _spawn = async (job_: FlowJob) => {
            job_.opts = {
              ...job_.opts,
              parent: {
                id: job.id!,
                queue: job.queueQualifiedName,
              },
            };
            await flowProducer.add(job_);
          };

          try {
            const processedR = await processor({
              job,
              token,
              logger,
              workingDirToBeUploadedToCloudStorage: workingDir,
              ensureLocalSourceFileExists,
              saveToTextFile,
              spawnChildJobsToWaitOn,
              update,
            });
            // await job.updateProgress(processedR as object);
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: queueName,
              jobId: job.id!,
              dbConn: db,
              jobStatus: "completed",
            });
            return processedR;
          } catch (e: any) {
            if (e instanceof WaitingChildrenError) {
              await _upsertAndMergeJobLogByIdAndType({
                projectId,
                jobType: queueName,
                jobId: job.id!,
                dbConn: db,
                jobStatus: "waiting_children",
              });
            } else {
              await _upsertAndMergeJobLogByIdAndType({
                projectId,
                jobType: queueName,
                jobId: job.id!,
                dbConn: db,
                jobStatus: "failed",
              });
              throw e;
            }
          }
        }
      },
      mergedWorkerOptions
    );

    worker.on("active", (job: Job) => {});

    worker.on("failed", async (job, error: Error) => {
      logger.error(`JOB FAILED: ${job?.id}, ${error}`);
    });

    worker.on("error", (err) => {
      logger.error(`ERROR: ${err}`);
    });

    worker.on("progress", (job: Job, progress: number | object) => {});

    worker.on("completed", async (job: Job, result: R) => {
      logger.info(`JOB COMPLETED: ${job.id}`);
    });

    worker.run();
    logger.info(`${queueName} worker started.`);

    return { worker, ...queueFuncs };
  };

  return { mainFn };
}

type ArgumentTypes<F extends Function> = F extends (...args: infer A) => any
  ? A
  : never;
