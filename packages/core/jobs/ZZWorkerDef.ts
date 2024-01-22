import { ZZProcessor } from "./ZZJob";
import { ZZJobSpec } from "./ZZJobSpec";
import { ZZEnv } from "./ZZEnv";
import { z } from "zod";
import { ZZWorker } from "./ZZWorker";

export type ZZWorkerDefParams<P, IMap, OMap, WP extends object = {}> = {
  concurrency?: number;
  jobSpec: ZZJobSpec<P, IMap, OMap>;
  processor: ZZProcessor<ZZJobSpec<P, IMap, OMap>, WP>;
  instanceParamsDef?: z.ZodType<WP>;
  zzEnv?: ZZEnv;
};

export class ZZWorkerDef<P, IMap = {}, OMap = {}, WP extends object = {}> {
  public readonly jobSpec: ZZJobSpec<P, IMap, OMap>;
  public readonly instanceParamsDef?: z.ZodType<WP | {}>;
  public readonly processor: ZZProcessor<ZZJobSpec<P, IMap, OMap>, WP>;
  public readonly zzEnv: ZZEnv | null = null;

  constructor({
    jobSpec,
    processor,
    instanceParamsDef,
    zzEnv,
  }: ZZWorkerDefParams<P, IMap, OMap, WP>) {
    this.jobSpec = jobSpec;
    this.instanceParamsDef = instanceParamsDef || z.object({});
    this.processor = processor;
    this.zzEnv = zzEnv || jobSpec.zzEnv;
  }

  public async startWorker(p?: {
    concurrency?: number;
    instanceParams?: WP;
    zzEnv?: ZZEnv;
  }) {
    const { concurrency, instanceParams } = p || {};

    const worker = new ZZWorker<P, IMap, OMap, WP>({
      def: this,
      concurrency,
      instanceParams: instanceParams || ({} as WP),
      zzEnv: p?.zzEnv || this.zzEnv,
    });
    // this.workers.push(worker);
    await worker.waitUntilReady();
    return worker;
  }
  public static define<P, IMap, OMap, WP extends object>(
    p: Omit<ZZWorkerDefParams<P, IMap, OMap, WP>, "jobSpec"> & {
      jobSpec:
        | ConstructorParameters<typeof ZZJobSpec<P, IMap, OMap>>[0]
        | ZZJobSpec<P, IMap, OMap>;
    }
  ) {
    const spec = new ZZJobSpec<P, IMap, OMap>(p.jobSpec);
    return spec.defineWorker(p);
  }

  public enqueueJob: (typeof this.jobSpec)["enqueueJob"] = (p) => {
    return this.jobSpec.enqueueJob(p);
  };

  public enqueueJobAndGetOutputs: (typeof this.jobSpec)["enqueueJobAndGetOutputs"] =
    (p) => {
      return this.jobSpec.enqueueJobAndGetOutputs(p);
    };
}
