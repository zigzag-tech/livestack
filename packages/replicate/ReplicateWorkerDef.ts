import Replicate from "replicate";
import { ZZJobSpec } from "@livestack/core";
import { ZZWorkerDef } from "@livestack/core";
import { ZZEnv } from "@livestack/core";

const TIMEOUT_IN_SECONDS = 60 * 15; // 15 minutes

export class ReplicateWorkerDef<P extends object, O> extends ZZWorkerDef<
  P,
  {},
  { default: O },
  { replicateToken?: string }
> {
  protected _endpoint: `${string}/${string}:${string}`;
  protected _replicateToken?: string;
  constructor({
    endpoint,
    concurrency = 3,
    jobSpec,
    zzEnv,
    replicateToken,
  }: {
    jobSpec: ZZJobSpec<P, {}, { default: O }>;
    endpoint: `${string}/${string}:${string}`;
    concurrency?: number;
    zzEnv?: ZZEnv;
    replicateToken?: string;
  }) {
    super({
      zzEnv,
      jobSpec,
      concurrency,
      processor: async ({ jobOptions, workerInstanceParams }) => {
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
        const repR = replicate.run(this._endpoint, {
          input: jobOptions,
        }) as Promise<unknown> as Promise<O>;

        const result = await Promise.race([
          repR,
          timeout(TIMEOUT_IN_SECONDS * 1000),
        ]);

        if (!result) {
          throw new Error(
            `no result returned from replicate endpoint: ${this._endpoint}`
          );
        }

        return result;
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
