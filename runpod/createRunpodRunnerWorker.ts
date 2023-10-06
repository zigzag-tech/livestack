import { createWorkerMainFunction } from "../microworkers/worker-creator";
import { GenericRecordType, QueueName } from "../microworkers/workerCommon";
import { Knex } from "knex";
import { WorkerOptions } from "bullmq";
import { IStorageProvider } from "../storage/cloudStorage";

if (!process.env.REPLICATE_API_TOKEN) {
  throw new Error("REPLICATE_API_TOKEN not found");
}

export function createRunpodRunnerWorker<
  TJobData extends object,
  TJobResult,
  QueueNameDef extends GenericRecordType
>({
  endpointId,
  queueName,
  queueNamesDef,
  projectId,
  db,
  workerOptions,
  runpodApiKey,
  storageProvider,
}: {
  queueName: QueueName<QueueNameDef>;
  queueNamesDef: QueueNameDef;
  endpointId: string;
  projectId: string;
  db: Knex;
  workerOptions: WorkerOptions;
  runpodApiKey: string;
  storageProvider: IStorageProvider;
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

      const runUrl = `https://api.runpod.ai/v2/${endpointId}/run`;

      const respP = await fetch(runUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${runpodApiKey}`,
        },
        body: JSON.stringify({
          input: input,
        }),
      });

      if (!respP.ok) {
        const errorBody = await respP.text();
        throw new Error(
          `Fetch to runpod serverless endpoint ${endpointId} failed with status ${respP.status}: ${errorBody}`
        );
      }
      type RunpodResult = { id: string; delayTime: number } & (
        | {
            status: "IN_PROGRESS";
          }
        | {
            status: "IN_QUEUE";
          }
        | {
            status: "CANCELLED";
          }
        | {
            status: "FAILED";
          }
        | {
            status: "COMPLETED";
            executionTime: number;
            output: TJobResult;
          }
      );

      let runpodResult = (await respP.json()) as RunpodResult;

      const statusUrl = `https://api.runpod.ai/v2/${endpointId}/status/${runpodResult.id}`;
      while (
        runpodResult.status === "IN_PROGRESS" ||
        runpodResult.status === "IN_QUEUE"
      ) {
        await sleep(1500);
        logger.info(`Checking status for job ${runpodResult.id}...`);
        const respP = await fetch(statusUrl, {
          method: "GET",
          headers: {
            Authorization: `Bearer ${runpodApiKey}`,
          },
        });

        if (!respP.ok) {
          const errorBody = await respP.text();
          throw new Error(
            `Fetch to runpod serverless endpoint ${endpointId} failed with status ${respP.status}: ${errorBody}`
          );
        }

        runpodResult = await respP.json();
        logger.info(
          `Status for job ${runpodResult.id}: ${runpodResult.status}`
        );
      }

      if (runpodResult.status === "CANCELLED") {
        throw new Error(`Runpod job ${runpodResult.id} was cancelled`);
      } else if (runpodResult.status === "FAILED") {
        throw new Error(`Runpod job ${runpodResult.id} failed`);
      }

      await update({
        incrementalData: {
          runpodResult: runpodResult.output,
          status: "FINISH",
          updateTime: Date.now(),
        },
      });

      return { runpodResult: runpodResult.output };
    },
    storageProvider,
  });
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
