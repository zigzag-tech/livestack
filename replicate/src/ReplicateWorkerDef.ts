import Replicate from "replicate";
import { JobSpec } from "@livestack/core";
import { LiveWorkerDef } from "@livestack/core";
import { LiveEnv } from "@livestack/core";
import { InferDefaultOrSingleKey } from "@livestack/core";

const TIMEOUT_IN_SECONDS = 60 * 15; // 15 minutes

export class ReplicateWorkerDef<
  I,
  O,
  IMap,
  OMap,
  KI extends keyof IMap,
  KO extends keyof OMap
> extends LiveWorkerDef<any, I, O, { replicateToken?: string }, IMap, OMap> {
  protected _endpoint: `${string}/${string}:${string}`;
  protected _replicateToken?: string;
  constructor({
    endpoint,
    concurrency = 3,
    jobSpec,
    liveEnv,
    replicateToken,
    inputTag,
    outputTag,
    autostartWorker = true,
  }: {
    jobSpec: JobSpec<any, I, O, IMap, OMap>;
    endpoint: `${string}/${string}:${string}`;
    concurrency?: number;
    liveEnv?: LiveEnv;
    replicateToken?: string;
    inputTag?: KI;
    outputTag?: KO;
    autostartWorker?: boolean;
  }) {
    super({
      liveEnv,
      jobSpec,
      concurrency,
      autostartWorker,
      processor: async ({ logger, input, output, workerInstanceParams }) => {
        const replicateToken =
          workerInstanceParams?.replicateToken ||
          this._replicateToken ||
          process.env.REPLICATE_API_TOKEN;
        if (!replicateToken) {
          throw new Error(
            "No replicate token provided. Please specify it in the worker instance params, job parameter or in the environment variable REPLICATE_API_TOKEN."
          );
        }
        const replicate = new Replicate({
          auth: process.env.REPLICATE_API_TOKEN,
        });
        const params = (await input(inputTag).nextValue())!;
        const repR = replicate.run(this._endpoint, {
          input: params,
        }) as Promise<unknown> as Promise<
          OMap[KO extends never ? InferDefaultOrSingleKey<OMap> : KO]
        >;
        logger.info("Sending request to replicate endpoint...");
        const result = await Promise.race([
          repR,
          timeout(TIMEOUT_IN_SECONDS * 1000),
        ]);

        if (!result) {
          throw new Error(
            `no result returned from replicate endpoint: ${this._endpoint}`
          );
        }
        logger.info("Replicate result received.");

        await output(outputTag).emit(result);
      },
    });
    this._endpoint = endpoint;
    this._replicateToken = replicateToken;
  }
}

function timeout(timeoutInMilliseconds: number) {
  return new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error("Timeout")), timeoutInMilliseconds)
  );
}
