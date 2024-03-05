import { InstantiatedGraph, JobId } from "@livestack/shared";
import { InferTMap } from "@livestack/shared";
import { vaultClient } from "@livestack/vault-client";
import _ from "lodash";
import { Observable, map, takeUntil } from "rxjs";
import { z } from "zod";
import { TransformRegistry } from "../orchestrations/TransformRegistry";
import {
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "../realtime/pubsub";
import { IStorageProvider, getPublicCdnUrl } from "../storage/cloudStorage";
import { getLogger } from "../utils/createWorkerLogger";
import { WrapTerminatorAndDataId } from "../utils/io";
import { longStringTruncator } from "../utils/longStringTruncator";
import { DataStream } from "./DataStream";
import { JobSpec } from "./JobSpec";
import { ZZEnv } from "./ZZEnv";
import { identifyLargeFilesToSave } from "../files/file-ops";

export type ZZProcessor<P, I, O, WP extends object, IMap, OMap> = (
  j: ZZJob<P, I, O, WP, IMap, OMap>
) => Promise<OMap[keyof OMap] | void>;

export interface ByTagCallable<TMap> {
  <K extends keyof TMap>(key?: K): {
    nextValue: () => Promise<TMap[K] | null>;
    [Symbol.asyncIterator](): AsyncGenerator<TMap[K]>;
  };
}

export class ZZJob<
  P,
  I,
  O,
  WP extends object = {},
  IMap = InferTMap<I>,
  OMap = InferTMap<O>
> {
  readonly jobOptions: P;

  readonly logger: ReturnType<typeof getLogger>;
  readonly spec: JobSpec<P, I, O, IMap, OMap>;
  public graph: InstantiatedGraph;

  //async iterator
  readonly input: ReturnType<typeof this.genInputObject> &
    ByTagCallable<IMap> & {
      tags: (keyof IMap)[];
      byTag: <K extends keyof IMap>(
        tag: K
      ) => {
        nextValue: () => Promise<IMap[K] | null>;
        [Symbol.asyncIterator](): AsyncGenerator<IMap[K]>;
      };
    };

  // New properties for subscriber tracking

  readonly output: {
    <K extends keyof OMap>(key?: K): {
      emit: (o: OMap[K]) => Promise<void>;
      getStreamId: () => Promise<string>;
    };
    emit: (o: OMap[keyof OMap]) => Promise<void>;
    getStreamId: () => Promise<string>;
    byTag: <K extends keyof OMap>(
      tag: K
    ) => {
      emit: (o: OMap[K]) => Promise<void>;
      getStreamId: () => Promise<string>;
    };
  };

  storageProvider?: IStorageProvider;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  public jobId: JobId;
  public readonly workerName;
  private readonly inputStreamFnsByTag: Partial<{
    [K in keyof IMap]: {
      nextValue: () => Promise<IMap[K] | null>;
      // inputStream: DataStream<WrapTerminatorAndDataId<IMap[K]>>;
      inputObservableUntracked: Observable<IMap[K] | null>;
      trackedObservable: Observable<IMap[K] | null>;
      subscriberCountObservable: Observable<number>;
    };
  }>;
  private updateProgress: (count: number) => Promise<void>;

  constructor(p: {
    logger: ReturnType<typeof getLogger>;
    jobOptions: P;
    storageProvider?: IStorageProvider;
    jobSpec: JobSpec<P, I, O, IMap, OMap>;
    workerInstanceParams?: WP;
    workerInstanceParamsSchema?: z.ZodType<WP>;
    workerName: string;
    graph: InstantiatedGraph;
    jobId: JobId;
    updateProgress: (count: number) => Promise<void>;
  }) {
    this.jobId = p.jobId as JobId;
    this.logger = p.logger;
    this.workerName = p.workerName;
    this.spec = p.jobSpec;
    this.graph = p.graph;
    this.updateProgress = p.updateProgress;

    try {
      this.jobOptions = p.jobSpec.jobOptions.parse(p.jobOptions) as P;
    } catch (err) {
      this.logger.error(
        `jobOptions error: jobOptions provided is invalid: ${JSON.stringify(
          err,
          null,
          2
        )}`
      );
      throw err;
    }

    try {
      this.workerInstanceParams = (p.workerInstanceParamsSchema?.parse(
        p.workerInstanceParams
      ) ||
        p.workerInstanceParams ||
        null) as WP extends object ? WP : null;
    } catch (err) {
      this.logger.error(
        `workerInstanceParams error: data provided is invalid: ${JSON.stringify(
          err
        )}`
      );
      throw err;
    }

    this.storageProvider = p.storageProvider;
    this.zzEnv = p.jobSpec.zzEnvEnsured;
    this.spec = p.jobSpec;

    this.inputStreamFnsByTag = {};

    // const reportOnReady = (
    //   obs: Observable<IMap[keyof IMap] | null>,
    //   tag: keyof IMap
    // ) => {
    //   const sub = obs.subscribe(async () => {
    //     await this.setJobReadyForInputsInRedis({
    //       jobId: this.jobId,
    //       isReady: true,
    //       tag: tag ? tag : this.spec.getSingleInputTag(),
    //     });
    //     sub.unsubscribe();
    //   });
    // };

    this.input = (() => {
      const func = <K extends keyof IMap>(tag: K) => {
        const obj = this.genInputObjectByTag(tag);
        // reportOnReady(obj.getObservable(), tag);
        return obj;
      };

      func.byTag = <K extends keyof IMap>(tag: K) => {
        const obj = this.genInputObjectByTag(tag);
        // reportOnReady(obj.getObservable(), tag);
        return obj;
      };

      const obj = this.genInputObject();
      Object.assign(func, obj);

      return func as any;
    })();

    const emitOutput = async <K extends keyof OMap>(
      o: OMap[K],
      tag = this.spec.getSingleOutputTag() as K
    ) => {
      // this.logger.info(
      //   `Emitting output: ${this.jobId}, ${this.def.name} ` +
      //     JSON.stringify(o, longStringTruncator)
      // );

      await this.spec._getStreamAndSendDataToPLimited({
        jobId: this.jobId,
        type: "out",
        tag: tag,
        data: {
          data: o,
          terminate: false,
        },
      });

      await this.updateProgress(this._dummyProgressCount++);
    };
    const that = this;
    this.output = (() => {
      const func = <K extends keyof OMap>(tag?: K) => ({
        emit: (o: OMap[K]) => emitOutput(o, tag),
        async getStreamId() {
          if (!tag) {
            tag = that.spec.getSingleOutputTag() as K;
          }
          const s = await that.spec.getOutputJobStream({
            jobId: that.jobId,
            tag,
          });
          return s.uniqueName;
        },
      });
      func.byTag = <K extends keyof OMap>(tag: K) => ({
        emit: (o: OMap[K]) => emitOutput(o, tag),
        async getStreamId() {
          const s = await that.spec.getOutputJobStream({
            jobId: that.jobId,
            tag,
          });
          return s.uniqueName;
        },
      });
      func.emit = (o: OMap[keyof OMap]) => {
        const tag = this.spec.getSingleOutputTag();
        return emitOutput(o, tag as keyof OMap);
      };
      func.getStreamId = async () => {
        const tag = this.spec.getSingleOutputTag();

        const s = await that.spec.getOutputJobStream({
          jobId: that.jobId,
          tag,
        });
        return s.uniqueName;
      };
      func;
      return func;
    })();
  }

  private readonly genInputObject = () => {
    return {
      ...this.genInputObjectByTag(),
      tags: this.spec.inputTags,
    };
  };

  private readonly genInputObjectByTag = <K extends keyof IMap>(tag?: K) => {
    const that = this;
    const nextValue = async () => {
      const r = await (await that._ensureInputStreamFn(tag)).nextValue();
      return r as IMap[K] | null;
    };
    return {
      nextValue,
      // getObservable() {
      //   if (!tag) {
      //     tag = that.spec.getSingleInputTag() as K;
      //   }
      //   return that._ensureInputStreamFn(tag).trackedObservable;
      // },
      async *[Symbol.asyncIterator]() {
        if (!tag) {
          tag = that.spec.getSingleInputTag() as K;
        }
        while (true) {
          const input = await nextValue();

          // Assuming nextInput returns null or a similar value to indicate completion
          if (!input) {
            break;
          }
          yield input;
        }
      },
    };
  };

  private _parentRec:
    | (Omit<
        NonNullable<
          Awaited<ReturnType<typeof vaultClient.db.getParentJobRec>>["rec"]
        >,
        "job_params_str"
      > & {
        job_params: any;
      })
    | null
    | "uninitialized" = "uninitialized";

  private getParentRec = async () => {
    if (this._parentRec === "uninitialized") {
      const { null_response, rec } = await vaultClient.db.getParentJobRec({
        projectId: this.zzEnv.projectId,
        childJobId: this.jobId,
      });
      if (!rec) {
        this._parentRec = null;
      } else {
        this._parentRec = {
          ...rec,
          job_params: rec.job_params_str
            ? JSON.parse(rec.job_params_str)
            : undefined,
        };
      }
    }
    return this._parentRec;
  };

  private _ensureInputStreamFn = <K extends keyof IMap>(tag?: K) => {
    if (this.spec.inputTags.length === 0) {
      throw new Error("inputDefs is empty for spec " + this.spec.name);
    }
    if (this.spec.isInputSingle) {
      tag = this.spec.getSingleInputTag() as K;
    } else {
      if (!tag) {
        throw new Error(
          `inputDefs consists of multiple streams ${this.spec.inputTags.join(
            ", "
          )}, but key is not provided.`
        );
      }
    }

    if (!this.inputStreamFnsByTag[tag!]) {
      const streamP = this.spec.getInputJobStream({
        jobId: this.jobId,
        tag: tag!,
      }) as Promise<DataStream<WrapTerminatorAndDataId<IMap[K] | unknown>>>;
      const parentRecP = this.getParentRec();

      const inputObservableUntracked = new Observable<IMap[K] | null>((s) => {
        Promise.all([streamP, parentRecP]).then(([stream, parentRec]) => {
          const sub = stream.subFromBeginning();
          const obs = sub.valueObservable.pipe(
            map((x) => (x.terminate ? null : x.data))
          );
          obs.subscribe((n) => {
            if (n) {
              let r: IMap[K];

              // find any transform function defined for this input
              // and apply it if found

              const transform = !parentRec
                ? null
                : TransformRegistry.getTransform({
                    receivingSpecName: this.spec.name,
                    tag: tag!.toString(),
                    workflowSpecName: parentRec.spec_name,
                    receivingSpecUniqueLabel:
                      parentRec.unique_spec_label || null,
                  });
              if (transform) {
                r = transform(n);
              } else {
                r = n as IMap[K];
              }
              s.next(r);
            } else {
              s.next(null);
              s.complete();
            }
          });
        });

        return () => {
          s.unsubscribe();
        };
      });

      const { trackedObservable, subscriberCountObservable } =
        createTrackedObservable(inputObservableUntracked);

      const { nextValue } = createLazyNextValueGenerator(trackedObservable);

      this.inputStreamFnsByTag[tag as K] = {
        nextValue,
        // inputStream: stream,
        inputObservableUntracked,
        trackedObservable,
        subscriberCountObservable,
      };
    }
    return this.inputStreamFnsByTag[tag as K]!;
  };

  public beginProcessing = async (
    processor: ZZProcessor<P, I, O, WP, IMap, OMap>
  ): Promise<void> => {
    const jId = {
      specName: this.spec.name,
      jobId: this.jobId,
      projectId: this.zzEnv.projectId,
    };

    const logger = this.logger;
    const projectId = this.zzEnv.projectId;

    logger.info(
      `Job started. Job ID: ${this.jobId}.` +
        `${JSON.stringify(this.jobOptions, longStringTruncator)}`
    );

    try {
      await vaultClient.db.appendJobStatusRec({
        ...jId,
        jobStatus: "running",
      });
      const allInputUnsubscribed = Promise.all(
        (
          _.values(
            this.inputStreamFnsByTag
          ) as (typeof this.inputStreamFnsByTag)[keyof IMap][]
        ).map(async (x) => {
          await new Promise<void>((resolve) => {
            x!.subscriberCountObservable
              .pipe(takeUntil(x!.inputObservableUntracked))
              .subscribe((count) => {
                console.log("count", count);
                if (count === 0) {
                  resolve();
                }
              });
          });
        })
      );
      const processedR = await processor(this);
      // console.debug("processed", this.jobId);

      // wait as long as there are still subscribers
      await allInputUnsubscribed;

      if (processedR && this.spec.isOutputSingle) {
        await this.output.emit(processedR);
      }

      await vaultClient.db.appendJobStatusRec({
        ...jId,
        jobStatus: "completed",
      });

      // await job.updateProgress(processedR as object);
      // console.debug("signalOutputEnd", this.jobId);
      for (const tag of this.spec.outputDefSet.tags) {
        await this.signalOutputEnd(tag);
      }

      // if (processedR) {
      //   return processedR;
      // }
    } catch (e: any) {
      await vaultClient.db.appendJobStatusRec({
        projectId,
        specName: this.spec.name,
        jobId: this.jobId,
        jobStatus: "failed",
      });
      // throw e;
      // }
    }
  };

  signalOutputEnd = async (tag?: keyof OMap) => {
    // console.debug("signalOutputEnd", {
    //   jobSpec: this.spec.name,
    //   jobId: this.jobId,
    //   tag,
    // });
    const outputStream = await this.spec.getOutputJobStream({
      jobId: this.jobId,
      tag: tag || this.spec.getSingleOutputTag(),
    });
    await outputStream.pub({
      message: {
        terminate: true,
      },
    });
  };

  // public saveToTextFile = async ({
  //   relativePath,
  //   data,
  // }: {
  //   relativePath: string;
  //   data: string;
  // }) => {
  //   await ensurePathExists(this.dedicatedTempWorkingDir);
  //   fs.writeFileSync(
  //     path.join(this.dedicatedTempWorkingDir, relativePath),
  //     data
  //   );
  // };

  getLargeValueCdnUrl = async <T extends object>(key: keyof T, obj: T) => {
    if (!this.storageProvider) {
      throw new Error("storageProvider is not provided");
    }
    if (!this.storageProvider.getPublicUrl) {
      throw new Error("storageProvider.getPublicUrl is not provided");
    }
    const { largeFilesToSave } = identifyLargeFilesToSave(obj);
    const found = largeFilesToSave.find((x) => x.path === key);
    if (!found) {
      console.error("Available keys: ", Object.keys(obj));
      throw new Error(`Cannot find ${String(key)} in largeFilesToSave`);
    } else {
      return getPublicCdnUrl({
        projectId: this.zzEnv.projectId,
        jobId: this.jobId,
        key: String(key),
        storageProvider: this.storageProvider,
      });
    }
  };
}
