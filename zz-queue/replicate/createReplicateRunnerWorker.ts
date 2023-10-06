import { createWorkerMainFunction } from "../microworkers/worker-creator";
import Replicate from "replicate";
import { GenericRecordType, QueueName } from "../microworkers/workerCommon";
import { Knex } from "knex";
import { WorkerOptions } from "bullmq";
import { IStorageProvider } from "../storage/cloudStorage";

if (!process.env.REPLICATE_API_TOKEN) {
  throw new Error("REPLICATE_API_TOKEN not found");
}
const replicate = new Replicate({
  auth: process.env.REPLICATE_API_TOKEN,
});

export function createReplicateRunnerWorker<
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
  storageProvider,
}: {
  queueName: QueueName<QueueNameDef>;
  queueNamesDef: QueueNameDef;
  endpoint: `${string}/${string}:${string}`;
  projectId: string;
  db: Knex;
  workerOptions: WorkerOptions;
  storageProvider?: IStorageProvider;
}) {
  return createWorkerMainFunction<
    TJobData,
    { replicateResult: TJobResult },
    QueueNameDef
  >({
    queueName,
    queueNamesDef,
    projectId,
    db,
    workerOptions,
    processor: async ({ job, logger, update }) => {
      const input = job.data;
      const result = (await replicate.run(endpoint, {
        input: input,
      })) as unknown as TJobResult;

      await update({
        incrementalData: {
          replicateResult: result,
          status: "FINISH",

          updateTime: Date.now(),
        },
      });

      return { replicateResult: result };
    },
    storageProvider,
  });
}
