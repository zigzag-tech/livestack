import { PipeDef, ZZEnv } from "../microworkers/PipeDef";
import { ZZPipe } from "../microworkers/ZZPipe";
import { z } from "zod";

export interface AttemptDef<ParentP, ParentO, P, O> {
  def: PipeDef<P, O>;
  timeout: number;
  transformInput: (
    params: ParentP
  ) =>
    | Promise<z.infer<this["def"]["jobParams"]>>
    | z.infer<this["def"]["jobParams"]>;
  transformOutput: (
    output: z.infer<this["def"]["output"]>
  ) => Promise<ParentO> | ParentO;
}
export class ZZProgressiveAdaptiveTryPipe<P, O> extends ZZPipe<P, O> {
  attempts: AttemptDef<P, O, unknown, unknown>[];
  constructor({
    zzEnv,
    def,
    attempts,
    ultimateFallback,
  }: {
    zzEnv: ZZEnv;
    def: PipeDef<P, O>;
    attempts: AttemptDef<P, O, any, any>[];
    ultimateFallback?: () => Promise<O>;
  }) {
    super({
      zzEnv,
      def,
      processor: async ({ logger, jobParams, spawnJob, jobId }) => {
        const genRetryFunction = <NewP, NewO>({
          def,
          transformInput,
          transformOutput,
        }: AttemptDef<P, O, NewP, NewO>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${def.name}`;

            const { nextOutput } = await spawnJob({
              jobId: childJobId,
              def: def,
              jobParams: await transformInput(jobParams),
            });

            const o = await nextOutput();
            if (!o) {
              throw new Error("no output");
            }

            const result = await transformOutput(o);

            return {
              timeout: false as const,
              error: false as const,
              result,
            };
          };
          return fn;
        };

        const restToTry = attempts.map((a) => ({
          fn: genRetryFunction(a),
          timeout: a.timeout,
          name: a.def.name,
        }));

        let promises: {
          promise: Promise<
            | Awaited<ReturnType<(typeof restToTry)[0]["fn"]>>
            | { error: true; timeout: false }
          >;
          timeout: number;
        }[] = [];

        while (restToTry.length > 0) {
          const m = restToTry.shift()!;
          logger.info(
            `Trying ${m.name}(${restToTry.length} more to attempt)...`
          );
          promises.push({
            promise: m.fn().catch((e) => {
              console.log(e);

              logger.warn("");
              return {
                timeout: false,
                error: true as const,
              };
            }),
            timeout: m.timeout,
          });

          const r = await Promise.race([
            ...promises.map((p) => p.promise),
            genTimeoutPromise(m.timeout),
          ]);
          if (!r.timeout && !r.error) {
            return r.result;
          } else if (r.timeout) {
            logger.info(`Timeout for ${m.name}. Moving on...`);
          }
        }

        if (ultimateFallback) {
          return await ultimateFallback();
        } else {
          throw new Error("All retries failed.");
        }
      },
    });

    this.attempts = attempts;
  }
}

export const genTimeoutPromise = async (timeout: number) => {
  const timeoutPromise = new Promise<void>((resolve) => {
    setTimeout(() => {
      resolve();
    }, timeout);
  });
  await timeoutPromise;
  return { timeout: true as const, error: false as const };
};
