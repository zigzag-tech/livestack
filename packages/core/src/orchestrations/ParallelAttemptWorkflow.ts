import { InferStreamSetType } from "@livestack/shared";
import { CheckSpec, JobSpec } from "../jobs/JobSpec";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { ZZEnv } from "../jobs/ZZEnv";
import { WrapWithTimestamp } from "../utils/io";
import sleep from "../utils/sleep";
type TriggerCheckContext = {
  totalTimeElapsed: number;
  attemptStats: {
    name: string;
    attemptTimeElapsed: number;
    resolved: boolean;
  }[];
};

export interface ParallelAttempt<
  ParentI,
  ParentO,
  ParentIMap,
  ParentOMap,
  IMap,
  OMap
> {
  jobSpec: JobSpec<unknown, ParentI, ParentO, IMap, OMap>;
  timeout: number;
  transformInput: (
    params: ParentIMap[keyof ParentIMap]
  ) => Promise<IMap[keyof IMap]> | IMap[keyof IMap];
  transformOutput: <K extends keyof ParentOMap>(
    // TODO: fix this type
    // output: OMap[K]
    output: OMap[keyof OMap]
  ) => Promise<ParentOMap[K]> | ParentOMap[K];
  triggerCondition: (c: TriggerCheckContext) => boolean;
}

export class ParallelAttemptWorkflow<
  ParentI,
  ParentO,
  ParentIMap,
  ParentOMap,
  Specs
> extends ZZWorkerDef<any, ParentI, ParentO, {}, ParentIMap, ParentOMap> {
  constructor({
    attempts,
    globalTimeoutCondition,
    transformCombinedOutput,
    jobSpec: parentJobSpec,
    zzEnv,
  }: {
    zzEnv?: ZZEnv;
    jobSpec: JobSpec<any, ParentI, ParentO, ParentIMap, ParentOMap>;
    globalTimeoutCondition?: (c: TriggerCheckContext) => boolean;
    transformCombinedOutput: (
      results: {
        result?: any;
        timedout: boolean;
        error: boolean;
        name: string;
      }[]
    ) => Promise<ParentOMap[keyof ParentOMap]> | ParentOMap[keyof ParentOMap];
    attempts: {
      [K in keyof Specs]: ParallelAttempt<
        ParentI,
        ParentO,
        ParentIMap,
        ParentOMap,
        InferStreamSetType<CheckSpec<Specs[K]>["inputDefSet"]>,
        InferStreamSetType<CheckSpec<Specs[K]>["outputDefSet"]>
      >;
    };
  }) {
    super({
      zzEnv,
      jobSpec: parentJobSpec,
      processor: async ({ logger, input: parentInput, jobId }) => {
        const genRetryFunction = <I, O>({
          jobSpec,
          transformInput,
          transformOutput,
        }: ParallelAttempt<ParentI, ParentO, ParentIMap, ParentOMap, I, O>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${jobSpec.name}`;
            const { output, input } = await jobSpec.enqueueJob({
              jobId: childJobId,
            });

            const inp = await parentInput.nextValue();
            if (!inp) {
              throw new Error("no input");
            }

            await input.feed(await transformInput(inp));

            const o = await output.nextValue();
            if (!o) {
              throw new Error("no output");
            }

            const result = {
              ...o,
              data: await transformOutput(o.data),
            };

            return {
              resolved: true as const,
              error: false as const,
              result,
            };
          };
          return fn;
        };

        const contexts: TriggerCheckContext[] = [];
        const restAttempts = [
          ...(attempts as Array<ParallelAttempt<any, any, any, any, any, any>>),
        ];
        const running: {
          promise: ReturnType<ReturnType<typeof genRetryFunction>>;
          name: string;
          timeStarted: number;
          isResolved: () => boolean;
          getResult: () =>
            | WrapWithTimestamp<ParentOMap[keyof ParentOMap]>
            | undefined;
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

            let result:
              | WrapWithTimestamp<ParentOMap[keyof ParentOMap]>
              | undefined = undefined;
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
