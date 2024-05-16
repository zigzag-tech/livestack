import { JobSpec } from "../jobs/JobSpec";
import { InferDefaultOrSingleKey, LiveWorkerDef } from "../jobs/LiveWorker";
import { LiveEnv } from "../env/LiveEnv";
import { WrapWithTimestamp } from "../utils/io";
import { sleep } from "../utils/sleep";
type TriggerCheckContext = {
  totalTimeElapsed: number;
  attemptStats: {
    name: string;
    attemptTimeElapsed: number;
    resolved: boolean;
  }[];
};

export interface ParallelAttempt<ParentIMap, ParentOMap, I, O, IMap, OMap> {
  jobSpec: JobSpec<any, I, O, IMap, OMap>;
  timeout?: number;
  transformInput: (
    params: ParentIMap[InferDefaultOrSingleKey<ParentIMap>]
  ) =>
    | Promise<NoInfer<IMap[InferDefaultOrSingleKey<IMap>]>>
    | NoInfer<IMap[InferDefaultOrSingleKey<IMap>]>;
  transformOutput: (
    output: OMap[InferDefaultOrSingleKey<OMap>]
  ) =>
    | Promise<ParentOMap[InferDefaultOrSingleKey<ParentOMap>]>
    | ParentOMap[InferDefaultOrSingleKey<ParentOMap>];
  triggerCondition: (c: TriggerCheckContext) => boolean;
}

export class ParallelAttemptLiveflow<
  ParentI,
  ParentO,
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2 = never,
  O2 = never,
  IMap2 = never,
  OMap2 = never,
  I3 = never,
  O3 = never,
  IMap3 = never,
  OMap3 = never,
  I4 = never,
  O4 = never,
  IMap4 = never,
  OMap4 = never,
  I5 = never,
  O5 = never,
  IMap5 = never,
  OMap5 = never
> extends LiveWorkerDef<any, ParentI, ParentO, {}, ParentIMap, ParentOMap> {
  constructor({
    attempts,
    globalTimeoutCondition,
    transformCombinedOutput,
    jobSpec: parentJobSpec,
    autostartWorker = true,
    liveEnv,
  }: {
    liveEnv?: LiveEnv;
    autostartWorker?: boolean;
    jobSpec: JobSpec<any, ParentI, ParentO, ParentIMap, ParentOMap>;
    globalTimeoutCondition?: (c: TriggerCheckContext) => boolean;
    transformCombinedOutput: (
      results: {
        result?: any;
        timedout: boolean;
        error: boolean;
        name: string;
      }[]
    ) =>
      | Promise<ParentOMap[InferDefaultOrSingleKey<ParentOMap>]>
      | ParentOMap[InferDefaultOrSingleKey<ParentOMap>];
    attempts:
      | AttemptsUpTo5<
          ParentIMap,
          ParentOMap,
          I1,
          O1,
          IMap1,
          OMap1,
          I2,
          O2,
          IMap2,
          OMap2,
          I3,
          O3,
          IMap3,
          OMap3,
          I4,
          O4,
          IMap4,
          OMap4,
          I5,
          O5,
          IMap5,
          OMap5
        >
      | ParallelAttempt<ParentIMap, ParentOMap, any, any, any, any>;
  }) {
    super({
      liveEnv,
      jobSpec: parentJobSpec,
      autostartWorker,
      workerPrefix: "paral-try",
      processor: async ({ logger, input: parentInput, jobId }) => {
        const genRetryFunction = <I, O, IMap, OMap>({
          jobSpec,
          transformInput,
          transformOutput,
        }: ParallelAttempt<ParentIMap, ParentOMap, I, O, IMap, OMap>) => {
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
            | WrapWithTimestamp<ParentOMap[InferDefaultOrSingleKey<ParentOMap>]>
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
              | WrapWithTimestamp<
                  ParentOMap[InferDefaultOrSingleKey<ParentOMap>]
                >
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

type Attemp2<
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2,
  O2,
  IMap2,
  OMap2
> = [
  ParallelAttempt<ParentIMap, ParentOMap, I1, O1, IMap1, OMap1>,
  ParallelAttempt<ParentIMap, ParentOMap, I2, O2, IMap2, OMap2>
];

type Attemp3<
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2,
  O2,
  IMap2,
  OMap2,
  I3,
  O3,
  IMap3,
  OMap3
> = [
  ...Attemp2<
    ParentIMap,
    ParentOMap,
    I1,
    O1,
    IMap1,
    OMap1,
    I2,
    O2,
    IMap2,
    OMap2
  >,
  ParallelAttempt<ParentIMap, ParentOMap, I3, O3, IMap3, OMap3>
];

type Attemp4<
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2,
  O2,
  IMap2,
  OMap2,
  I3,
  O3,
  IMap3,
  OMap3,
  I4,
  O4,
  IMap4,
  OMap4
> = [
  ...Attemp3<
    ParentIMap,
    ParentOMap,
    I1,
    O1,
    IMap1,
    OMap1,
    I2,
    O2,
    IMap2,
    OMap2,
    I3,
    O3,
    IMap3,
    OMap3
  >,
  ParallelAttempt<ParentIMap, ParentOMap, I4, O4, IMap4, OMap4>
];

type Attemp5<
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2,
  O2,
  IMap2,
  OMap2,
  I3,
  O3,
  IMap3,
  OMap3,
  I4,
  O4,
  IMap4,
  OMap4,
  I5,
  O5,
  IMap5,
  OMap5
> = [
  ...Attemp4<
    ParentIMap,
    ParentOMap,
    I1,
    O1,
    IMap1,
    OMap1,
    I2,
    O2,
    IMap2,
    OMap2,
    I3,
    O3,
    IMap3,
    OMap3,
    I4,
    O4,
    IMap4,
    OMap4
  >,
  ParallelAttempt<ParentIMap, ParentOMap, I5, O5, IMap5, OMap5>
];

type AttemptsUpTo5<
  ParentIMap,
  ParentOMap,
  I1,
  O1,
  IMap1,
  OMap1,
  I2,
  O2,
  IMap2,
  OMap2,
  I3,
  O3,
  IMap3,
  OMap3,
  I4,
  O4,
  IMap4,
  OMap4,
  I5,
  O5,
  IMap5,
  OMap5
> =
  | [ParallelAttempt<ParentIMap, ParentOMap, I1, O1, IMap1, OMap1>]
  | Attemp2<ParentIMap, ParentOMap, I1, O1, IMap1, OMap1, I2, O2, IMap2, OMap2>
  | Attemp3<
      ParentIMap,
      ParentOMap,
      I1,
      O1,
      IMap1,
      OMap1,
      I2,
      O2,
      IMap2,
      OMap2,
      I3,
      O3,
      IMap3,
      OMap3
    >
  | Attemp4<
      ParentIMap,
      ParentOMap,
      I1,
      O1,
      IMap1,
      OMap1,
      I2,
      O2,
      IMap2,
      OMap2,
      I3,
      O3,
      IMap3,
      OMap3,
      I4,
      O4,
      IMap4,
      OMap4
    >
  | Attemp5<
      ParentIMap,
      ParentOMap,
      I1,
      O1,
      IMap1,
      OMap1,
      I2,
      O2,
      IMap2,
      OMap2,
      I3,
      O3,
      IMap3,
      OMap3,
      I4,
      O4,
      IMap4,
      OMap4,
      I5,
      O5,
      IMap5,
      OMap5
    >;
