import { _upsertAndMergeJobLogByIdAndType } from "../db/knexConn";
import fs from "fs";
import _ from "lodash";
import {
  Worker,
  Job,
  Processor,
  FlowProducer,
  WaitingChildrenError,
} from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { getMicroworkerQueueByName, longStringTruncator } from "./queues";
import { GenericRecordType, QueueName } from "./workerCommon";
import { getJobLogByIdAndType } from "../db/knexConn";
import { WorkerOptions } from "bullmq";
import { Knex } from "knex";
import { TEMP_DIR, getTempPathByJobId } from "../storage/temp-dirs";
import { ensurePathExists } from "../storage/ensurePathExists";
import { getStorageBucket } from "../db/cloudStorage";
import path from "path";
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
    flowProducer: FlowProducer;
    logger: ReturnType<typeof getLogger>;
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

          const update = async ({
            incrementalData,
          }: {
            incrementalData?: any;
          }) => {
            await _upsertAndMergeJobLogByIdAndType({
              projectId,
              jobType: queueName,
              jobId: job.id!,
              jobData: incrementalData,
              dbConn: db,
            });
          };
          try {
            const processedR = await processor({
              job,
              token,
              flowProducer,
              logger,
              workingDirToBeUploadedToCloudStorage: workingDir,
              ensureLocalSourceFileExists,
              saveToTextFile,
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
