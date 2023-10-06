import fs from "fs";
import os from "os";
import path from "path";
import { GenericRecordType, QueueName } from "./workerCommon";
import { ConnectionOptions, QueueEvents } from "bullmq";
import { getStorageBucket } from "../db/cloudStorage";
import { getLogger } from "../utils/createWorkerLogger";
import { Knex } from "knex";

const logger = getLogger("data-logging");
const OS_TEMP_DIR = os.tmpdir();

export const initializeAndLogQueueEvents = <T extends GenericRecordType>({
  queueName,
  projectId,
  db,
  redisConn,
  bucketName,
}: {
  queueName: QueueName<T>;
  projectId: string;
  db: Knex;
  redisConn: ConnectionOptions;
  bucketName: string;
}) => {
  const queueEvents = new QueueEvents(queueName, {
    connection: redisConn,
  });
  queueEvents.on("completed", async ({ jobId, returnvalue: data }) => {
    // await db("jobs_log")
    //   .insert({
    //     project_id: projectId,
    //     job_type: queueName,
    //     job_id: jobId,
    //     job_data: JSON.stringify(data || {}),
    //   })
    //   .onConflict(["job_id", "job_type", "project_id"])
    //   .ignore();

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

      const storageBucket = getStorageBucket(bucketName);
      await Promise.all(
        jobFiles.map(async (f) => {
          const p = path.join(jobTmpDir, f);
          try {
            const destination = p
              .split(`${OS_TEMP_DIR}/`)[1]
              .replace(/_/g, "/");
            await storageBucket.upload(p, { destination });
          } catch (error) {
            logger.error(
              `Failed to upload file ${p}. Job ID: ${jobId}, ${JSON.stringify(
                error
              )}`
            );
          }
        })
      );
    }
  }
};
