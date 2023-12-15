import { first } from "lodash";
import { ZZWorker } from "../microworkers/ZZWorker";
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

const TIMEOUT_IN_SECONDS = 60 * 15; // 15 minutes

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
    concurrency = 3,
  }: {
    queueName: string;
    endpoint: `${string}/${string}:${string}`;
    projectId: string;
    db: Knex;
    redisConfig: RedisOptions;
    storageProvider?: IStorageProvider;
    concurrency?: number;
  }) {
    super({
      db,
      redisConfig,
      storageProvider,
      projectId,
      queueName,
      concurrency,
      processor: async ({ params, logger, update, emitOutput }) => {
        const P = replicate.run(this._endpoint, {
          input: params,
        }) as Promise<unknown> as Promise<TJobResult>;

        const result = await Promise.race([
          P,
          timeout(TIMEOUT_IN_SECONDS * 1000),
        ]);

        if (!result) {
          throw new Error(
            `no result returned from replicate endpoint: ${this._endpoint}`
          );
        }
        // const result = sampleResult as any;

        await update({
          incrementalData: {
            replicateResult: result,
            status: "FINISH",
          } as Partial<
            TJobData & { status: "FINISH"; replicateResult: TJobResult }
          >,
        });

        await emitOutput({
          replicateResult: result,
        });

        return { replicateResult: result };
      },
    });
    this._endpoint = endpoint;
  }
}

function timeout(timeoutInMilliseconds: number) {
  return new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error("Timeout")), timeoutInMilliseconds)
  );
}

const sampleResult = {
  json_data: {
    mask: [
      { label: "background", value: 0 },
      {
        box: [
          4.59039306640625, 4.681396484375, 762.9484252929688,
          1018.3617553710938,
        ],
        label: "alphabet",
        logit: 0.39,
        value: 1,
      },
      {
        box: [
          304.52850341796875, 539.1644897460938, 503.0081481933594,
          1020.618408203125,
        ],
        label: "crayon",
        logit: 0.37,
        value: 2,
      },
      {
        box: [
          30.5687255859375, 635.8360595703125, 656.815673828125,
          1021.2097778320312,
        ],
        label: "child",
        logit: 0.35,
        value: 3,
      },
      {
        box: [
          29.861785888671875, 636.03564453125, 330.3247375488281,
          1021.3016357421875,
        ],
        label: "child",
        logit: 0.29,
        value: 4,
      },
      {
        box: [
          2.1947174072265625, 6.9219207763671875, 284.86163330078125,
          269.14691162109375,
        ],
        label: "alphabet",
        logit: 0.26,
        value: 5,
      },
      {
        box: [
          315.8265686035156, 203.28353881835938, 498.3658447265625,
          513.41845703125,
        ],
        label: "crayon",
        logit: 0.25,
        value: 6,
      },
    ],
    tags: "alphabet, art, child, crayon, draw, drawing, mark, writing",
  },
  masked_img: null,
  rounding_box_img: null,
  tags: "alphabet, art, child, crayon, draw, drawing, mark, writing",
};
