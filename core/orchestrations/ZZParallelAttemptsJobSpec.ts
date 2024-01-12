import { ZZJobSpec, sleep } from "../microworkers/ZZJobSpec";
import { ZZWorkerDef } from "../microworkers/ZZWorker";

type TriggerCheckContext = {
  totalTimeElapsed: number;
  attemptStats: {
    name: string;
    attemptTimeElapsed: number;
    resolved: boolean;
  }[];
};

export interface ParallelAttempt<ParentP, ParentI, ParentO> {
  jobSpec: ZZJobSpec<ParentP, { default: ParentI }, { default: ParentO }>;
  triggerCondition: (c: TriggerCheckContext) => boolean;
}

export function genParallelAttempt<ParentP, ParentI, ParentO>(
  jobSpec: ZZJobSpec<ParentP, { default: ParentI }, { default: ParentO }>,
  config: Omit<ParallelAttempt<ParentP, ParentI, ParentO>, "jobSpec">
): ParallelAttempt<ParentP, ParentI, ParentO> {
  return {
    jobSpec,
    ...config,
  };
}

export class ZZParallelAttemptWorkerDef<P, I, O> extends ZZWorkerDef<
  P,
  I extends unknown
    ? unknown
    : {
        default: I;
      },
  {
    default: O;
  }
> {
  constructor({
    attempts,
    globalTimeoutCondition,
    transformCombinedOutput,
    jobSpec,
  }: {
    jobSpec: ZZJobSpec<
      P,
      I extends unknown
        ? unknown
        : {
            default: I;
          },
      {
        default: O;
      }
    >;

    globalTimeoutCondition?: (c: TriggerCheckContext) => boolean;
    transformCombinedOutput: (
      results: {
        result?: any;
        timedout: boolean;
        error: boolean;
        name: string;
      }[]
    ) => Promise<O> | O;
    attempts: ParallelAttempt<P, I, O>[];
  }) {
    super({
      jobSpec,
      processor: async ({ logger, jobParams, jobId }) => {
        const genRetryFunction = ({
          jobSpec: attemptDef,
        }: ParallelAttempt<P, I, O>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${attemptDef.name}`;
            const { outputs } = await jobSpec.requestJob({
              jobId: childJobId,
              jobParams: jobParams as P,
            });

            const o = await outputs.nextValue();
            if (!o) {
              throw new Error("no output");
            }

            const result = o;

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
            logger.info(`Started attempt ${nextAttempt.jobSpec.name}.`);
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
              name: nextAttempt.jobSpec.name,
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
