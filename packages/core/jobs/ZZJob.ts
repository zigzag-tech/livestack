import { WrapTerminatorAndDataId } from "../utils/io";
import { DataStream } from "./DataStream";
import { Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "../realtime/pubsub";
import { Observable, map, takeUntil } from "rxjs";
import { IStorageProvider } from "../storage/cloudStorage";
import { updateJobStatus } from "@livestack/vault-dev-server/src/db/jobs";
import { getParentJobRec } from "@livestack/vault-dev-server/src/db/job_relations";
import longStringTruncator from "../utils/longStringTruncator";
import { JobSpec } from "./JobSpec";
import { z } from "zod";
import Redis, { RedisOptions } from "ioredis";
import { ZZEnv } from "./ZZEnv";
import { InferTMap } from "@livestack/shared/IOSpec";
import _ from "lodash";
import { InstantiatedGraph, JobId } from "../orchestrations/InstantiatedGraph";
import { RawQueueJobData } from "../orchestrations/workerCommon";
import { TransformRegistry } from "../orchestrations/TransformRegistry";

export type ZZProcessor<P, I, O, WP extends object, IMap, OMap> = (
  j: ZZJob<P, I, O, WP, IMap, OMap>
) => Promise<OMap[keyof OMap] | void>;

export interface ByTagCallable<TMap> {
  <K extends keyof TMap>(key: K): {
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
  private readonly bullMQJob: Job<RawQueueJobData<P>, void>;
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
    <K extends keyof OMap>(key: K): {
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

  constructor(p: {
    bullMQJob: Job<RawQueueJobData<P>, void, string>;
    logger: ReturnType<typeof getLogger>;
    jobOptions: P;
    storageProvider?: IStorageProvider;
    jobSpec: JobSpec<P, I, O, IMap, OMap>;
    workerInstanceParams?: WP;
    workerInstanceParamsSchema?: z.ZodType<WP>;
    workerName: string;
    graph: InstantiatedGraph;
  }) {
    this.bullMQJob = p.bullMQJob;
    this.jobId = this.bullMQJob.id! as JobId;
    this.logger = p.logger;
    this.workerName = p.workerName;
    this.spec = p.jobSpec;
    this.graph = p.graph;

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
    //       redisConfig: this.zzEnv.redisConfig,
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

      await this.bullMQJob.updateProgress(this._dummyProgressCount++);
    };
    const that = this;
    this.output = (() => {
      const func = <K extends keyof OMap>(tag: K) => ({
        emit: (o: OMap[K]) => emitOutput(o, tag),
        async getStreamId() {
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
      tags: this.spec.inputDefSet.keys,
    };
  };

  private readonly genInputObjectByTag = <K extends keyof IMap>(tag?: K) => {
    const that = this;
    const nextValue = async (tag0?: K) => {
      const r = await (await that._ensureInputStreamFn(tag0)).nextValue();
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
          const input = await nextValue(tag);

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
    | Awaited<ReturnType<typeof getParentJobRec>>
    | "uninitialized" = "uninitialized";

  private getParentRec = async () => {
    if (this._parentRec === "uninitialized") {
      this._parentRec = await getParentJobRec({
        projectId: this.zzEnv.projectId,
        dbConn: this.zzEnv.db,
        childJobId: this.jobId,
      });
    }
    return this._parentRec;
  };

  private _ensureInputStreamFn<K extends keyof IMap>(tag?: K) {
    if (this.spec.inputDefSet.isSingle) {
      tag = this.spec.getSingleInputTag() as K;
    } else {
      if (!tag) {
        throw new Error(
          `inputDefs is multiple streams, but key is not provided`
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

      subscriberCountObservable.subscribe((count) => {
        if (count > 0) {
          this.setJobReadyForInputsInRedis({
            redisConfig: this.zzEnv.redisConfig,
            jobId: this.jobId,
            isReady: true,
            tag: tag || this.spec.getSingleInputTag(),
          });
        }
      });

      this.inputStreamFnsByTag[tag as K] = {
        nextValue,
        // inputStream: stream,
        inputObservableUntracked,
        trackedObservable,
        subscriberCountObservable,
      };
    }
    return this.inputStreamFnsByTag[tag as K]!;
  }

  setJobReadyForInputsInRedis = async ({
    redisConfig,
    jobId,
    tag,
    isReady,
  }: {
    redisConfig: RedisOptions;
    jobId: string;
    tag: keyof IMap;
    isReady: boolean;
  }) => {
    // console.debug("setJobReadyForInputsInRedis", {
    //   jobSpec: this.jobSpec.name,
    //   jobId,
    //   key,
    //   isReady,
    // });
    if (!tag) {
      throw new Error("key is required");
    }
    try {
      const redis = new Redis(redisConfig);
      await redis.set(
        `ready_status__${jobId}/${String(tag)}`,
        isReady ? "true" : "false"
      );
    } catch (error) {
      console.error("Error setJobReadyForInputsInRedis:", error);
    }
  };

  public beginProcessing = async (
    processor: ZZProcessor<P, I, O, WP, IMap, OMap>
  ): Promise<void> => {
    const jId = {
      specName: this.spec.name,
      jobId: this.jobId,
      projectId: this.zzEnv.projectId,
    };

    const job = this.bullMQJob;
    const logger = this.logger;
    const projectId = this.zzEnv.projectId;

    logger.info(
      `Job started. Job ID: ${job.id}.` +
        `${JSON.stringify(job.data, longStringTruncator)}`,
      +`${JSON.stringify(await job.getChildrenValues(), longStringTruncator)}`
    );

    try {
      if (this.zzEnv.db) {
        await updateJobStatus({
          ...jId,
          dbConn: this.zzEnv.db,
          jobStatus: "running",
        });
      }
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

      if (processedR && this.spec.outputDefSet.isSingle) {
        await this.output.emit(processedR);
      }

      if (this.zzEnv.db) {
        await updateJobStatus({
          ...jId,
          dbConn: this.zzEnv.db,
          jobStatus: "completed",
        });
      }

      // await job.updateProgress(processedR as object);
      // console.debug("signalOutputEnd", this.jobId);
      for (const tag of this.spec.outputDefSet.keys) {
        await this.signalOutputEnd(tag);
      }

      // if (processedR) {
      //   return processedR;
      // }
    } catch (e: any) {
      if (this.zzEnv.db) {
        await updateJobStatus({
          projectId,
          specName: this.spec.name,
          jobId: job.id!,
          dbConn: this.zzEnv.db,
          jobStatus: "failed",
        });
      }
      throw e;
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

  // getLargeValueCdnUrl = async <T extends object>(key: keyof T, obj: T) => {
  //   if (!this.storageProvider) {
  //     throw new Error("storageProvider is not provided");
  //   }
  //   if (!this.storageProvider.getPublicUrl) {
  //     throw new Error("storageProvider.getPublicUrl is not provided");
  //   }
  //   const { largeFilesToSave } = identifyLargeFiles(obj);
  //   const found = largeFilesToSave.find((x) => x.path === key);
  //   if (!found) {
  //     console.error("Available keys: ", Object.keys(obj));
  //     throw new Error(`Cannot find ${String(key)} in largeFilesToSave`);
  //   } else {
  //     return getPublicCdnUrl({
  //       projectId: this.zzEnv.projectId,
  //       jobId: this.jobId,
  //       key: String(key),
  //       storageProvider: this.storageProvider,
  //     });
  //   }
  // };
}
