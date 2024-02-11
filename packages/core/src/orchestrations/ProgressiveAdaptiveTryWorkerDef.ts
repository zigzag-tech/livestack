import { CheckSpec, JobSpec } from "../jobs/JobSpec";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import _ from "lodash";
import { InferStreamSetType, InferTMap } from "@livestack/shared";
import { ZZEnv } from "../jobs/ZZEnv";
import { genTimeoutPromise } from "../utils/genTimeoutPromise";

export interface AttemptDef<ParentIMap, ParentOMap, I, O, IMap, OMap> {
  jobSpec: JobSpec<any, I, O, IMap, OMap>;
  timeout?: number;
  transformInput: <K extends keyof ParentIMap>(
    data: ParentIMap[K]
  ) => Promise<IMap[keyof IMap]> | IMap[keyof IMap];
  transformOutput: <K extends keyof OMap>(
    output: OMap[K]
  ) => Promise<ParentOMap[keyof ParentOMap]> | ParentOMap[keyof ParentOMap];
}

export class ProgressiveAdaptiveTryWorkerDef<
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
> extends ZZWorkerDef<any, ParentI, ParentO, {}, ParentIMap, ParentOMap> {
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
    | AttemptDef<ParentIMap, ParentOMap, any, any, any, any>;
  constructor({
    zzEnv,
    attempts,
    ultimateFallback,
    jobSpec,
  }: {
    zzEnv?: ZZEnv;
    jobSpec: JobSpec<any, ParentI, ParentO, ParentIMap, ParentOMap>;
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
      | AttemptDef<ParentIMap, ParentOMap, any, any, any, any>;
    ultimateFallback?: () => Promise<ParentOMap[keyof ParentOMap]>;
  }) {
    super({
      jobSpec,
      zzEnv,
      workerPrefix: "prog-adaptive-try",
      processor: async ({ input, jobId }) => {
        const genRetryFunction = <I, O, IMap, OMap>({
          jobSpec,
          transformInput,
          transformOutput,
        }: AttemptDef<ParentIMap, ParentOMap, I, O, IMap, OMap>) => {
          const fn = async () => {
            const childJobId = `${jobId}/${jobSpec.name}`;
            const inpt = (await input.nextValue())!;
            const jo = await jobSpec.enqueueJob({
              jobId: childJobId,
              parentJobId: jobId,
            });
            const transformed = await transformInput(inpt);
            await jo.input.feed(transformed as IMap[keyof IMap]);

            const o = await jo.output.nextValue();
            if (!o) {
              throw new Error("no output");
            }

            return {
              timeout: false as const,
              error: false as const,
              result: (await transformOutput(
                o.data
              )) as ParentOMap[keyof ParentOMap],
            };
          };
          return fn;
        };

        const restToTry = (
          attempts as AttemptDef<ParentIMap, ParentOMap, any, any, any, any>[]
        ).map((a) => ({
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
          console.info(
            `Trying ${m.name} (${restToTry.length} remaining to attempt)...`
          );
          promises.push({
            promise: m.fn().catch((e) => {
              console.log(e);

              console.warn("");
              return {
                timeout: false,
                error: true as const,
              };
            }),
            timeout: m.timeout || 1000 * 60,
          });

          const r = await Promise.race([
            ...promises.map((p) => p.promise),
            genTimeoutPromise(m.timeout || 1000 * 60),
          ]);
          if (!r.timeout && !r.error) {
            return r.result;
          } else if (r.timeout) {
            console.info(`Timeout for ${m.name}. Moving on...`);
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
  AttemptDef<ParentIMap, ParentOMap, I1, O1, IMap1, OMap1>,
  AttemptDef<ParentIMap, ParentOMap, I2, O2, IMap2, OMap2>
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
  AttemptDef<ParentIMap, ParentOMap, I3, O3, IMap3, OMap3>
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
  AttemptDef<ParentIMap, ParentOMap, I4, O4, IMap4, OMap4>
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
  AttemptDef<ParentIMap, ParentOMap, I5, O5, IMap5, OMap5>
];

export type AttemptsUpTo5<
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
  | [AttemptDef<ParentIMap, ParentOMap, I1, O1, IMap1, OMap1>]
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
