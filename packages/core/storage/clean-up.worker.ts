// import fs from "fs";
// import { getTempPathByJobId } from "./temp-dirs";
// import { getMicroworkerQueueByName } from "../microworkers/queues";
// import { ZZWorker } from "../microworkers/ZZWorker";
// import { WorkerOptions } from "bullmq";
// import { Knex } from "knex";
// import { RedisOptions } from "ioredis";

// const repeatInterval = 60 * 60 * 1000;

// export const createCleanUpWorker = ({
//   projectId,
//   db,
//   redisConfig,
// }: {
//   projectId: string;
//   db: Knex;
//   redisConfig: RedisOptions;
// }) => {
//   class CleanUpWorker extends ZZWorker<any, any> {
//     constructor() {
//       super({
//         redisConfig,
//         projectId,
//         db,
//         color: "magenta",
//         queueName: ZZ_INTERNAL_QUEUE_NAMES.CLEAN_UP,
//         processor: async ({ logger }) => {
//           // Get records created over 24 hours ago
//           const interval = 24 * 60 * 60 * 1000;
//           const cutoffTime = new Date(Date.now() - interval);
//           const prevCutoffTime = new Date(
//             Date.now() - interval - repeatInterval
//           );

//           const rs = (await db("jobs_log")
//             .distinct("job_id")
//             .where("time_created", "<", cutoffTime)
//             .andWhere("time_created", ">=", prevCutoffTime)
//             .andWhereLike("job_id", `job_%`)) as { job_id: string }[];

//           for (const { job_id: jobId } of rs) {
//             const dirPath = getTempPathByJobId(jobId);
//             try {
//               fs.rmSync(dirPath, { recursive: true });
//               logger.info(`Deleted directory ${dirPath}`);
//             } catch (error: any) {
//               logger.info(
//                 `Error deleting directory ${dirPath}: ${error.message}`
//               );
//             }
//           }
//         },
//       });
//     }
//   }

//   const addRepeatCleanUpJob = async (workerOptions: WorkerOptions) => {
//     const { addJob } = getMicroworkerQueueByName({
//       queueName: ZZ_INTERNAL_QUEUE_NAMES.CLEAN_UP,
//       workerOptions,
//       db,
//       projectId,
//     });
//     await addJob({
//       jobId: "clean-up-tmp-dir",
//       jobOptions: { repeatPattern: `every ${repeatInterval / 1000} seconds` },
//       bullMQJobsOpts: {
//         repeat: { every: repeatInterval },
//       },
//     });
//   };

//   const cleanUpWorker = new CleanUpWorker();
//   return { ...cleanUpWorker, addRepeatCleanUpJob };
// };

// const ZZ_INTERNAL_QUEUE_NAMES = {
//   CLEAN_UP: "clean-up" as const,
// };

// // if (require.main === module) {
// //   // clear redis after test run to remove repeatable jobs
// //   const { worker, queue } = cleanUpWorkerMain();
// //   worker.on("completed", (job, result) => {
// //     console.info("completed", job.id, result);
// //   });
// //   queue.add(
// //     "clean-up-tmp-dir",
// //     { repeatPattern: `every 10 seconds` },
// //     {
// //       repeat: { every: 10 * 1000 },
// //     }
// //   );
// // }
