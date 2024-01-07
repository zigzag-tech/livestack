import { UnknownJobSpec, ZZJobSpec } from "../microworkers/ZZJobSpec";
import { z } from "zod";
import { ZZWorkerDef } from "../microworkers/ZZWorker";

export interface AttemptDef<
  MaJobSpecDef extends UnknownJobSpec,
  ParentP = z.infer<MaJobSpecDef["jobParamsDef"]>,
  ParentO = z.infer<MaJobSpecDef["outputDef"]>
> {
  def: MaJobSpecDef;
  timeout: number;
  transformInput: (params: ParentP) => Promise<ParentP> | ParentP;
  transformOutput: (
    output: z.infer<this["def"]["outputDef"]>
  ) => Promise<ParentO> | ParentO;
}
export class ZZProgressiveAdaptiveTryWorkerDef<
  MaJobSpec,
  P = z.infer<MaJobSpec["jobParamsDef"]>,
  O extends z.infer<MaJobSpec["outputDef"]> = z.infer<MaJobSpec["outputDef"]>
> extends ZZWorkerDef<MaJobSpec, {}> {
  attempts: AttemptDef<MaJobSpec>[];
  constructor({
    attempts,
    ultimateFallback,
    jobSpec,
  }: {
    jobSpec: ZZJobSpec<MaJobSpec>;
    attempts: AttemptDef<MaJobSpec>[];
    ultimateFallback?: () => Promise<O>;
  }) {
    super({
      jobSpec,
      processor: async ({ logger, jobParams, spawnJob, jobId }) => {
        const genRetryFunction = ({
          def,
          transformInput,
          transformOutput,
        }: AttemptDef<MaJobSpec>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${def.name}`;

            const { nextOutput } = await spawnJob({
              jobId: childJobId,
              def: def as ZZJobSpec<P, I, O, any, any, any>,
              jobParams: (await transformInput(jobParams)) as P,
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
