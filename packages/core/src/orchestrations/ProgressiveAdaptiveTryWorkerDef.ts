import { CheckSpec, JobSpec } from "../jobs/JobSpec";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import _ from "lodash";
import { InferStreamSetType, InferTMap } from "@livestack/shared";
import { ZZEnv } from "../jobs/ZZEnv";
import { genTimeoutPromise } from "../utils/genTimeoutPromise";

export interface AttemptDef<ParentIMap, ParentOMap, I, O, IMap, OMap> {
  jobSpec: JobSpec<any, I, O, IMap, OMap>;
  timeout: number;
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
  Specs
> extends ZZWorkerDef<any, ParentI, ParentO, {}, ParentIMap, ParentOMap> {
  attempts: {
    [K in keyof Specs]: AttemptDef<
      ParentIMap,
      ParentOMap,
      InferTMap<InferStreamSetType<CheckSpec<Specs[K]>["inputDefSet"]>>,
      InferTMap<InferStreamSetType<CheckSpec<Specs[K]>["outputDefSet"]>>,
      InferStreamSetType<CheckSpec<Specs[K]>["inputDefSet"]>,
      InferStreamSetType<CheckSpec<Specs[K]>["outputDefSet"]>
    >;
  };
  constructor({
    zzEnv,
    attempts,
    ultimateFallback,
    jobSpec,
  }: {
    zzEnv?: ZZEnv;
    jobSpec: JobSpec<any, ParentI, ParentO, ParentIMap, ParentOMap>;
    attempts: {
      [K in keyof Specs]: AttemptDef<
        ParentIMap,
        ParentOMap,
        InferTMap<InferStreamSetType<CheckSpec<Specs[K]>["inputDefSet"]>>,
        InferTMap<InferStreamSetType<CheckSpec<Specs[K]>["outputDefSet"]>>,
        InferStreamSetType<CheckSpec<Specs[K]>["inputDefSet"]>,
        InferStreamSetType<CheckSpec<Specs[K]>["outputDefSet"]>
      >;
    };
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
            timeout: m.timeout,
          });

          const r = await Promise.race([
            ...promises.map((p) => p.promise),
            genTimeoutPromise(m.timeout),
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
