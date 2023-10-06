import { ZZWorker } from "../microworkers/worker-creator-class";
import Replicate from "replicate";
import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import { RedisOptions } from "ioredis";

if (!process.env.REPLICATE_API_TOKEN) {
  throw new Error("REPLICATE_API_TOKEN not found");
}
const replicate = new Replicate({
  auth: process.env.REPLICATE_API_TOKEN,
});

export class ReplicateRunnerWorker<
  TJobData extends object,
  TJobResult
> extends ZZWorker<
  TJobData,
  {
    replicateResult: TJobResult;
  }
> {
  protected _endpoint: `${string}/${string}:${string}`;
  constructor({
    endpoint,
    queueName,
    projectId,
    db,
    redisConfig,
    storageProvider,
  }: {
    queueName: string;
    endpoint: `${string}/${string}:${string}`;
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
    this._endpoint = endpoint;
  }

  protected async processor({
    job,
    logger,
    update,
  }: Parameters<
    ZZWorker<
      TJobData,
      {
        replicateResult: TJobResult;
      }
    >["processor"]
  >[0]) {
    const input = job.data;
    const result = (await replicate.run(this._endpoint, {
      input: input,
    })) as unknown as TJobResult;

    await update({
      incrementalData: {
        replicateResult: result,
        status: "FINISH",
      } as Partial<
        TJobData & { status: "FINISH"; replicateResult: TJobResult }
      >,
    });

    return { replicateResult: result };
  }
}
