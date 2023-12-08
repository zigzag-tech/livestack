import { ZZWorker } from "../microworkers/worker-creator-class";
import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import { RedisOptions } from "ioredis";
import { first } from "lodash";

export class RunpodRunnerWorker<
  TJobData extends object,
  TJobResult
> extends ZZWorker<
  TJobData,
  {
    runpodResult: TJobResult;
  }
> {
  protected _endpointId: string;
  protected _runpodApiKey: string;

  constructor({
    endpointId,
    queueName,
    projectId,
    db,
    redisConfig,
    storageProvider,
    runpodApiKey,
  }: {
    queueName: string;
    endpointId: string;
    runpodApiKey: string;
    projectId: string;
    db: Knex;
    redisConfig: RedisOptions;
    storageProvider?: IStorageProvider;
  }) {
    super({
      db,
      redisConfig,
      storageProvider,
      projectId,
      queueName,
    });
    this._endpointId = endpointId;
    this._runpodApiKey = runpodApiKey;
  }

  protected async processor({
    firstInput,
    logger,
    update,
  }: Parameters<
    ZZWorker<
      TJobData,
      {
        runpodResult: TJobResult;
      }
    >["processor"]
  >[0]) {
    const runUrl = `https://api.runpod.ai/v2/${this._endpointId}/run`;

    const respP = await fetch(runUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this._runpodApiKey}`,
      },
      body: JSON.stringify({
        input: firstInput,
      }),
    });

    if (!respP.ok) {
      const errorBody = await respP.text();
      throw new Error(
        `Fetch to runpod serverless endpoint ${this._endpointId} failed with status ${respP.status}: ${errorBody}`
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

    const statusUrl = `https://api.runpod.ai/v2/${this._endpointId}/status/${runpodResult.id}`;
    while (
      runpodResult.status === "IN_PROGRESS" ||
      runpodResult.status === "IN_QUEUE"
    ) {
      await sleep(1500);
      logger.info(`Checking status for job ${runpodResult.id}...`);
      const respP = await fetch(statusUrl, {
        method: "GET",
        headers: {
          Authorization: `Bearer ${this._runpodApiKey}`,
        },
      });

      if (!respP.ok) {
        const errorBody = await respP.text();
        throw new Error(
          `Fetch to runpod serverless endpoint ${this._endpointId} failed with status ${respP.status}: ${errorBody}`
        );
      }

      runpodResult = await respP.json();
      logger.info(`Status for job ${runpodResult.id}: ${runpodResult.status}`);
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
      } as Partial<TJobData & { status: "FINISH"; runpodResult: TJobResult }>,
    });

    return { runpodResult: runpodResult.output };
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
