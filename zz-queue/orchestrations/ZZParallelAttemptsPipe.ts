import { InferPipeDef, PipeDef } from "../microworkers/PipeDef";
import { ZZPipe, sleep } from "../microworkers/ZZPipe";
import { z } from "zod";
import { ZZEnv } from "../microworkers/ZZEnv";

type TriggerCheckContext = {
  totalTimeElapsed: number;
  attemptStats: {
    name: string;
    attemptTimeElapsed: number;
    resolved: boolean;
  }[];
};

export interface ParallelAttempt<
  AttemptDef extends PipeDef<any, any, any, any, any>,
  ParentDef extends PipeDef<any, any, any, any, any>
> {
  def: AttemptDef;
  triggerCondition: (c: TriggerCheckContext) => boolean;
  transformInput: (
    params: z.infer<InferPipeDef<ParentDef>["jobParamsDef"]>
  ) =>
    | Promise<z.infer<InferPipeDef<AttemptDef>["jobParamsDef"]>>
    | z.infer<InferPipeDef<AttemptDef>["jobParamsDef"]>;
  transformOutput: (
    output: z.infer<AttemptDef["outputDef"]>
  ) =>
    | Promise<z.infer<AttemptDef["outputDef"]>>
    | z.infer<AttemptDef["outputDef"]>;
}

export function genParallelAttempt<
  AttemptDef extends PipeDef<any, any, any, any, any>,
  ParentDef extends PipeDef<any, any, any, any, any>
>(
  def: AttemptDef,
  parentDef: ParentDef,
  config: Omit<ParallelAttempt<AttemptDef, ParentDef>, "def">
): ParallelAttempt<AttemptDef, ParentDef> {
  return {
    def,
    ...config,
  };
}

export class ZZParallelAttemptsPipe<
  MaPipeDef extends PipeDef<any, any, any, any, any>,
  P = z.infer<InferPipeDef<MaPipeDef>["jobParamsDef"]>,
  O extends z.infer<InferPipeDef<MaPipeDef>["outputDef"]> = z.infer<
    InferPipeDef<MaPipeDef>["outputDef"]
  >
> extends ZZPipe<MaPipeDef> {
  attempts: ParallelAttempt<MaPipeDef, PipeDef<any, any, any, any, any>>[];

  constructor({
    zzEnv,
    def,
    attempts,
    globalTimeoutCondition,
    transformCombinedOutput,
  }: {
    zzEnv: ZZEnv;
    def: PipeDef<P, O, any, any, any>;
    attempts: ParallelAttempt<
      PipeDef<any, any, any, any, any>,
      PipeDef<P, O, any, any, any>
    >[];
    globalTimeoutCondition?: (c: TriggerCheckContext) => boolean;
    transformCombinedOutput: (
      results: {
        result?: any;
        timedout: boolean;
        error: boolean;
        name: string;
      }[]
    ) => Promise<O> | O;
  }) {
    super({
      zzEnv,
      def,
      processor: async ({ logger, jobParams, spawnJob, jobId }) => {
        const genRetryFunction = <NewP, NewO>({
          def: attemptDef,
          transformInput,
          transformOutput,
        }: ParallelAttempt<
          PipeDef<NewP, NewO, any, any, any>,
          PipeDef<P, O, any, any, any>
        >) => {
          const fn = async () => {
            const childJobId = `${jobId}/${attemptDef.name}`;
            const { nextOutput } = await spawnJob({
              jobId: childJobId,
              def: attemptDef,
              jobParams: await transformInput(jobParams),
            });

            const o = await nextOutput();
            if (!o) {
              throw new Error("no output");
            }

            const result = await transformOutput(o);

            return {
              resolved: true as const,
              error: false as const,
              result,
            };
          };
          return fn;
        };

        const contexts: TriggerCheckContext[] = [];
        const restAttempts = [...attempts];
        const running: {
          promise: ReturnType<ReturnType<typeof genRetryFunction>>;
          name: string;
          timeStarted: number;
          isResolved: () => boolean;
          getResult: () => O | undefined;
        }[] = [];

        const time = Date.now();
        const genCtx = () => {
          const ctx: TriggerCheckContext = {
            totalTimeElapsed: Date.now() - time,
            attemptStats: running.map((r) => ({
              name: r.name,
              attemptTimeElapsed: Date.now() - r.timeStarted,
              resolved: r.isResolved(),
            })),
          };
          contexts.push(ctx);
          return ctx;
        };

        if (!globalTimeoutCondition) {
          globalTimeoutCondition = (c) => {
            return c.totalTimeElapsed > 1000 * 60 * 15; // 15 minutes by default
          };
        }

        let cont = genCtx();

        do {
          // check head and see if is met for the first one
          let nextAttempt = restAttempts[0];
          if (!nextAttempt) {
            continue;
          }
          cont = genCtx();
          if (nextAttempt.triggerCondition(cont)) {
            nextAttempt = restAttempts.shift()!;
            const { setResolved, isResolved } = genResolvedTracker();

            let result: O | undefined = undefined;
            const fn = genRetryFunction(nextAttempt);
            this.logger.info(`Started attempt ${nextAttempt.def.name}.`);
            running.push({
              promise: fn()
                .then((r) => {
                  setResolved();
                  result = r.result;
                  return r;
                })
                .catch((e) => {
                  throw e;
                }),
              name: nextAttempt.def.name,
              timeStarted: Date.now(),
              isResolved: () => isResolved(),
              getResult: () => result,
            });
          }

          await sleep(200);
        } while (restAttempts.length > 0);

        await Promise.all(running.map((r) => r.promise));

        const raws = running.map((r) => {
          return {
            result: r.getResult(),
            timedout: false,
            error: false,
            name: r.name,
          };
        });

        return await transformCombinedOutput(raws);
      },
    });

    this.attempts = attempts;
  }
}

const genResolvedTracker = () => {
  let resolved = false;
  return {
    setResolved: () => {
      resolved = true;
    },
    isResolved: () => resolved,
  };
};

export const genTimeoutPromise = async (timeout: number) => {
  const timeoutPromise = new Promise<void>((resolve) => {
    setTimeout(() => {
      resolve();
    }, timeout);
  });
  await timeoutPromise;
  return { timeout: true as const, error: false as const };
};
