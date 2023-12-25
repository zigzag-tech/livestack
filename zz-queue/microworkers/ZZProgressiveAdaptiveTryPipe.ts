import { PipeDef, ZZEnv } from "./PipeRegistry";
import { ZZPipe } from "./ZZPipe";
import { z } from "zod";

export interface RetryDef<
  ParentDef extends PipeDef<ParentP, ParentO>,
  P,
  O,
  ParentP,
  ParentO
> {
  def: PipeDef<P, O>;
  timeout: number;
  transformInput: (params: ParentP) => P;
  transformOutput: (output: O) => ParentO;
}
export class ZZProgressiveAdaptiveTryPipe<P, O> extends ZZPipe<P, O> {
  retryDefs: RetryDef<PipeDef<P, O>, unknown, unknown, P, O>[];
  constructor({
    zzEnv,
    def,
    retryDefs,
    ultimateFallback,
  }: {
    zzEnv: ZZEnv;
    def: PipeDef<P, O>;
    retryDefs: RetryDef<PipeDef<P, O>, unknown, unknown, P, O>[];
    ultimateFallback?: () => Promise<O>;
  }) {
    super({
      zzEnv,
      def,
      processor: async ({ logger, initParams, spawnJob, jobId }) => {
        const genRetryFunction = <NewP, NewO>({
          def,
          transformInput,
          transformOutput,
        }: RetryDef<PipeDef<P, O>, NewP, NewO, P, O>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${def.name}`;

            const { nextOutput } = await spawnJob({
              jobId: childJobId,
              def: def,
              initParams: transformInput(initParams),
            });

            const o = await nextOutput();
            if (!o) {
              throw new Error("no output");
            }

            const result = transformOutput(o);

            return {
              timeout: false as const,
              error: false as const,
              result,
              engine: "coglvm" as const,
            };
          };
          return fn;
        };

        const restToTry = retryDefs.map((d) => ({
          fn: genRetryFunction(d),
          timeout: d.timeout,
          name: d.def.name,
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
          logger.info(`Trying ${m.name}...`);
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
          }
        }

        if (ultimateFallback) {
          return await ultimateFallback();
        } else {
          throw new Error("All retries failed.");
        }
      },
    });

    this.retryDefs = retryDefs;
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
