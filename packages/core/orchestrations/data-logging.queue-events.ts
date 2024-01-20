import fs from "fs";
import os from "os";
import path from "path";
import { GenericRecordType, QueueName } from "./workerCommon";
import { ConnectionOptions, QueueEvents } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import longStringTruncator from "../utils/longStringTruncator";

const logger = getLogger("data-logging");
const OS_TEMP_DIR = os.tmpdir();

export const initializeAndLogQueueEvents = <T extends GenericRecordType>({
  queueName,
  projectId,
  db,
  redisConn,
  storageProvider,
}: {
  queueName: QueueName<T>;
  projectId: string;
  db: Knex;
  redisConn: ConnectionOptions;
  bucketName: string;
  storageProvider: IStorageProvider;
}) => {
  const queueEvents = new QueueEvents(queueName, {
    connection: redisConn,
  });
  queueEvents.on("completed", async ({ jobId, returnvalue: data }) => {
    await saveAllWorkingFilesToCloudStorage(jobId);
  });

  queueEvents.on("failed", async ({ jobId, failedReason }) => {
    // logger.error(
    //   `${queueName} queueEvents listener failed for jobId ${jobId}. Reason: ${failedReason}`
    // );

    await saveAllWorkingFilesToCloudStorage(jobId);
  });

  async function saveAllWorkingFilesToCloudStorage(jobId: string) {
    const jobTmpDir = path.join(OS_TEMP_DIR, jobId);
    if (fs.existsSync(jobTmpDir)) {
      const jobFiles = fs.readdirSync(jobTmpDir);

      await Promise.all(
        jobFiles.map(async (f) => {
          const p = path.join(jobTmpDir, f);
          try {
            const destination = p
              .split(`${OS_TEMP_DIR}/`)[1]
              .replace(/_/g, "/");
            await storageProvider.uploadFromLocalPath({
              localPath: p,
              destination,
            });
          } catch (error) {
            logger.error(
              `Failed to upload file ${p}. Job ID: ${jobId}, ${JSON.stringify(
                error,
                longStringTruncator
              )}`
            );
          }
        })
      );
    }
  }
};
