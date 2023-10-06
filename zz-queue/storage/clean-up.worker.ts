import fs from "fs";
import { getTempPathByJobId } from "./temp-dirs";
import { getMicroworkerQueueByName } from "../microworkers/queues";
import { createWorkerMainFunction } from "../microworkers/worker-creator";
import { WorkerOptions } from "bullmq";
import { Knex } from "knex";

const repeatInterval = 60 * 60 * 1000;

export const createCleanUpWorker = ({
  projectId,
  db,
  workerOptions,
}: {
  projectId: string;
  db: Knex;
  workerOptions: WorkerOptions;
}) => {
  const { mainFn: cleanUpWorkerMain } = createWorkerMainFunction({
    queueName: "clean-up",
    queueNamesDef: ZZ_INTERNAL_QUEUE_NAMES,
    projectId,
    db,
    workerOptions,
    processor: async ({ logger }) => {
      // Get records created over 24 hours ago
      const interval = 24 * 60 * 60 * 1000;
      const cutoffTime = new Date(Date.now() - interval);
      const prevCutoffTime = new Date(Date.now() - interval - repeatInterval);

      const rs = (await db("jobs_log")
        .distinct("job_id")
        .where("time_created", "<", cutoffTime)
        .andWhere("time_created", ">=", prevCutoffTime)
        .andWhereLike("job_id", `job_%`)) as { job_id: string }[];

      for (const { job_id: jobId } of rs) {
        const dirPath = getTempPathByJobId(jobId);
        try {
          fs.rmSync(dirPath, { recursive: true });
          logger.info(`Deleted directory ${dirPath}`);
        } catch (error: any) {
          logger.info(`Error deleting directory ${dirPath}: ${error.message}`);
        }
      }
    },
    color: "magenta",
  });

  const addRepeatCleanUpJob = async (workerOptions: WorkerOptions) => {
    const { addJob } = getMicroworkerQueueByName({
      queueNamesDef: ZZ_INTERNAL_QUEUE_NAMES,
      queueName: ZZ_INTERNAL_QUEUE_NAMES.CLEAN_UP,
      workerOptions,
      db,
      projectId,
    });
    await addJob(
      "clean-up-tmp-dir",
      { repeatPattern: `every ${repeatInterval / 1000} seconds` },
      {
        repeat: { every: repeatInterval },
      }
    );
  };
  return { ...cleanUpWorkerMain(), addRepeatCleanUpJob };
};

const ZZ_INTERNAL_QUEUE_NAMES = {
  CLEAN_UP: "clean-up" as const,
};

// if (require.main === module) {
//   // clear redis after test run to remove repeatable jobs
//   const { worker, queue } = cleanUpWorkerMain();
//   worker.on("completed", (job, result) => {
//     console.info("completed", job.id, result);
//   });
//   queue.add(
//     "clean-up-tmp-dir",
//     { repeatPattern: `every 10 seconds` },
//     {
//       repeat: { every: 10 * 1000 },
//     }
//   );
// }
