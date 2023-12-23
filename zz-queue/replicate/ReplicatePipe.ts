import Replicate from "replicate";
import { ZZEnv } from "../microworkers/PipeRegistry";
import { PipeDef } from "../microworkers/PipeRegistry";
import { ZZPipe } from "../microworkers/ZZPipe";
import { z } from "zod";
if (!process.env.REPLICATE_API_TOKEN) {
  throw new Error("REPLICATE_API_TOKEN not found");
}

const TIMEOUT_IN_SECONDS = 60 * 15; // 15 minutes

export class ReplicatePipe<P extends object, O> extends ZZPipe<
  P,
  {
    replicateResult: O;
  }
> {
  protected _endpoint: `${string}/${string}:${string}`;

  constructor({
    endpoint,
    concurrency = 3,
    def,
    zzEnv,
  }: {
    endpoint: `${string}/${string}:${string}`;
    concurrency?: number;
    zzEnv: ZZEnv;
    def: PipeDef<P, O>;
  }) {
    super({
      def: def.derive({
        output: z.object({
          replicateResult: def.output,

          // TODO: fix any
        }) as any,
      }),
      concurrency,
      zzEnv,
      processor: async ({ initParams }) => {
        const replicate = new Replicate({
          auth: process.env.REPLICATE_API_TOKEN,
        });
        const repR = replicate.run(this._endpoint, {
          input: initParams,
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

        return { replicateResult: result! };
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
