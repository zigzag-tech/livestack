import { ZZJobSpec } from "../microworkers/ZZJobSpec";
import { ZZWorkerDef } from "../microworkers/ZZWorker";

export interface AttemptDef<ParentP, ParentI, ParentO> {
  jobSpec: ZZJobSpec<ParentP, { default: ParentI }, { default: ParentO }>;
  timeout: number;
  transformInput: (params: ParentP) => Promise<ParentP> | ParentP;
  transformOutput: (output: ParentO) => Promise<ParentO> | ParentO;
}
export class ZZProgressiveAdaptiveTryWorkerDef<P, I, O> extends ZZWorkerDef<
  P,
  { default: I },
  { default: O }
> {
  attempts: AttemptDef<P, I, O>[];
  constructor({
    attempts,
    ultimateFallback,
    jobSpec,
  }: {
    jobSpec: ZZJobSpec<P, { default: I }, { default: O }>;
    attempts: AttemptDef<P, I, O>[];
    ultimateFallback?: () => Promise<O>;
  }) {
    super({
      jobSpec,
      processor: async ({ logger, jobParams, jobId }) => {
        const genRetryFunction = ({
          jobSpec,
          transformInput,
          transformOutput,
        }: AttemptDef<P, I, O>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${jobSpec.name}`;

            await jobSpec.requestJob({
              jobId: childJobId,
              jobParams: (await transformInput(jobParams)) as P,
            });

            const jo = jobSpec.forJobOutput({ jobId: childJobId });

            const o = await jo.nextValue();
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
          name: a.jobSpec.name,
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
