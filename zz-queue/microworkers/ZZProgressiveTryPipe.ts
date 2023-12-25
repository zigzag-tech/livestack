import { PipeDef, ZZEnv } from "./PipeRegistry";
import { ZZPipe } from "./ZZPipe";

type RetryDef<P, O, NewP, NewO> = {
  def: PipeDef<P, O>;
  timeout: number;
  transformInput: (params: P) => NewP;
  transformOutput: (output: NewO) => O;
};
export class ZZProgressiveTryPipe<P, O> extends ZZPipe<P, O> {
  retryDefs: RetryDef<P, O, unknown, unknown>[];
  constructor({
    zzEnv,
    def,
    retryDefs,
  }: {
    zzEnv: ZZEnv;
    def: PipeDef<P, O>;
    retryDefs: {
      def: PipeDef<any, any, never, never, never>;
      timeout: number;
      transformInput: (params: any) => any;
      transformOutput: (output: any) => any;
    }[];
  }) {
    super({
      zzEnv,
      def,
      processor: async ({ logger, initParams, spawnJob, jobId }) => {
        const genRetryFunction = ({
          def,
          transformInput,
          transformOutput,
        }: {
          def: PipeDef<any, any, never, never, never>;
          transformInput: (params: any) => any;
          transformOutput: (output: any) => any;
        }) => {
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

        return { parsedCaption: "masterpiece", engine: "none" as const };
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
