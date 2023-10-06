import { createWorkerMainFunction } from "../microworkers/worker-creator";
import Replicate from "replicate";
import { GenericRecordType, QueueName } from "../microworkers/workerCommon";
import { Knex } from "knex";
import { WorkerOptions } from "bullmq";

if (!process.env.REPLICATE_API_TOKEN) {
  throw new Error("REPLICATE_API_TOKEN not found");
}
const replicate = new Replicate({
  auth: process.env.REPLICATE_API_TOKEN,
});

export function createRunpodRunnerWorker<
  TJobData extends object,
  TJobResult,
  QueueNameDef extends GenericRecordType
>({
  endpoint,
  queueName,
  queueNamesDef,
  projectId,
  db,
  workerOptions,
  runpodApiKey,
}: {
  queueName: QueueName<QueueNameDef>;
  queueNamesDef: QueueNameDef;
  endpoint: `${string}/${string}:${string}`;
  projectId: string;
  db: Knex;
  workerOptions: WorkerOptions;
  runpodApiKey: string;
}) {
  return createWorkerMainFunction<
    TJobData,
    { runpodResult: TJobResult },
    QueueNameDef
  >({
    queueName,
    queueNamesDef,
    projectId,
    db,
    workerOptions,
    processor: async ({ job, logger, update }) => {
      const input = job.data;

      const respP = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${runpodApiKey}`,
        },
        body: JSON.stringify({
          input: input,
        }),
      });

      const result = (await respP).json() as TJobResult;

      await update({
        incrementalData: {
          runpodResult: result,
          status: "FINISH",
          updateTime: Date.now(),
        },
      });

      return { runpodResult: result };
    },
    storageBucketName: "",
  });
}
