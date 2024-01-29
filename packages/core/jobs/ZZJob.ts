import { WrapTerminatorAndDataId } from "../utils/io";
import { ZZStream } from "./ZZStream";
import { Job, WaitingChildrenError } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "../realtime/pubsub";
import { Observable, map, takeUntil } from "rxjs";
import { IStorageProvider } from "../storage/cloudStorage";
import { updateJobStatus } from "../db/knexConn";
import longStringTruncator from "../utils/longStringTruncator";
import { ZZJobSpec } from "./ZZJobSpec";
import { z } from "zod";
import Redis, { RedisOptions } from "ioredis";
import { ZZEnv } from "./ZZEnv";
import { InferTMap } from "@livestack/shared/IOSpec";
import _ from "lodash";

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
  private readonly bullMQJob: Job<{ jobOptions: P }, void>;
  public readonly _bullMQToken?: string;
  readonly jobOptions: P;
  readonly logger: ReturnType<typeof getLogger>;
  readonly spec: ZZJobSpec<P, I, O, IMap, OMap>;
  // readonly nextInput: <K extends keyof IMap>(
  //   key?: K
  // ) => Promise<IMap[K] | null>;

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
    };
    emit: (o: OMap[keyof OMap]) => Promise<void>;
    byTag: <K extends keyof OMap>(
      tag: K
    ) => {
      emit: (o: OMap[K]) => Promise<void>;
    };
  };

  storageProvider?: IStorageProvider;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  public jobId: string;
  private readonly workerName;
  private readonly inputStreamFnsByTag: Partial<{
    [K in keyof IMap]: {
      nextValue: () => Promise<IMap[K] | null>;
      // inputStream: ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
      inputObservableUntracked: Observable<IMap[K] | null>;
      trackedObservable: Observable<IMap[K] | null>;
      subscriberCountObservable: Observable<number>;
    };
  }>;

  constructor(p: {
    bullMQJob: Job<{ jobOptions: P }, void, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    jobOptions: P;
    storageProvider?: IStorageProvider;
    jobSpec: ZZJobSpec<P, I, O, IMap, OMap>;
    workerInstanceParams?: WP;
    workerInstanceParamsSchema?: z.ZodType<WP>;
    workerName: string;
  }) {
    this.bullMQJob = p.bullMQJob;
    this.jobId = this.bullMQJob.id!;
    this._bullMQToken = p.bullMQToken;
    this.logger = p.logger;
    this.workerName = p.workerName;
    this.spec = p.jobSpec;

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

    const reportOnReady = (
      obs: Observable<IMap[keyof IMap] | null>,
      tag: keyof IMap
    ) => {
      const sub = obs.subscribe(async () => {
        await this.setJobReadyForInputsInRedis({
          redisConfig: this.zzEnv.redisConfig,
          jobId: this.jobId,
          isReady: true,
          tag: tag ? tag : this.spec.getSingleInputTag(),
        });
        sub.unsubscribe();
      });
    };

    this.input = (() => {
      const func = <K extends keyof IMap>(tag: K) => {
        const obj = this.genInputObjectByTag(tag);
        reportOnReady(obj.getObservable(), tag);
        return obj;
      };

      func.byTag = <K extends keyof IMap>(tag: K) => {
        const obj = this.genInputObjectByTag(tag);
        reportOnReady(obj.getObservable(), tag);
        return obj;
      };

      const obj = this.genInputObject();
      Object.assign(func, obj);

      return func as any;
    })();

    // this.input = {
    //   ...this.genInputObject(),
    //   byTag: <K extends keyof IMap>(key: K) => {
    //     const obj = this.genInputObjectByTag(key);
    //     reportOnReady(obj.observable, key);
    //     return obj;
    //   },
    // };
    if (this.spec.inputDefSet.isSingle) {
      reportOnReady(this.input.getObservable(), this.spec.getSingleInputTag());
    }

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

      this.bullMQJob.updateProgress(this._dummyProgressCount++);
    };
    this.output = (() => {
      const func = <K extends keyof OMap>(tag: K) => ({
        emit: (o: OMap[K]) => emitOutput(o, tag),
      });
      func.byTag = <K extends keyof OMap>(tag: K) => ({
        emit: (o: OMap[K]) => emitOutput(o, tag),
      });
      func.emit = (o: OMap[keyof OMap]) => {
        const tag = this.spec.getSingleOutputTag();
        return emitOutput(o, tag as keyof OMap);
      };
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
      getObservable() {
        if (!tag) {
          tag = that.spec.getSingleInputTag() as K;
        }
        return that._ensureInputStreamFn(tag).trackedObservable;
      },
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
      }) as Promise<ZZStream<WrapTerminatorAndDataId<IMap[K]>>>;

      const inputObservableUntracked = new Observable<IMap[K] | null>((s) => {
        streamP.then((stream) => {
          const sub = stream.subFromBeginning();
          const obs = sub.valueObservable.pipe(
            map((x) => (x.terminate ? null : x.data))
          );
          obs.subscribe((n) => {
            if (n) {
              s.next(n);
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

    // const savedResult = await getJobRec<OMap[keyof OMap]>({
    //   ...jId,
    //   dbConn: this.zzEnv.db,
    //   jobStatus: "completed",
    // });
    // if (savedResult) {
    //   const jobData = await getJobDataAndIoEvents<OMap[keyof OMap]>({
    //     ...jId,
    //     dbConn: this.zzEnv.db,
    //     ioType: "out",
    //   });
    //   this.logger.info(
    //     `Job already marked as complete; skipping: ${job.id}, ${this.bullMQJob.queueName} ` +
    //       `${JSON.stringify(this.bullMQJob.data, longStringTruncator)}`,
    //     +`${JSON.stringify(await job.getChildrenValues(), longStringTruncator)}`
    //   );

    //   // return last job data
    //   return jobData[jobData.length - 1]?.data || undefined;
    // } else {
    logger.info(
      `Job started: ${job.id}, ${job.queueName} ` +
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

      const processedR = await processor(this);
      // console.debug("processed", this.jobId);

      // wait as long as there are still subscribers
      await Promise.all(
        (
          _.values(
            this.inputStreamFnsByTag
          ) as (typeof this.inputStreamFnsByTag)[keyof IMap][]
        ).map(async (x) => {
          await new Promise<void>((resolve) => {
            x!.subscriberCountObservable
              .pipe(takeUntil(x!.inputObservableUntracked))
              .subscribe((count) => {
                if (count === 0) {
                  resolve();
                }
              });
          });
        })
      );

      if (processedR) {
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
      if (this.spec.outputDefSet.isSingle) {
        await this.signalOutputEnd();
      }

      // if (processedR) {
      //   return processedR;
      // }
    } catch (e: any) {
      if (e instanceof WaitingChildrenError) {
        if (this.zzEnv.db) {
          await updateJobStatus({
            projectId,
            specName: this.spec.name,
            jobId: job.id!,
            dbConn: this.zzEnv.db,
            jobStatus: "waiting_children",
          });
        }
      } else {
        if (this.zzEnv.db) {
          await updateJobStatus({
            projectId,
            specName: this.spec.name,
            jobId: job.id!,
            dbConn: this.zzEnv.db,
            jobStatus: "failed",
          });
        }
      }
      throw e;
      // }
    }
  };

  signalOutputEnd = async (tag?: keyof OMap) => {
    // console.debug("signalOutputEnd", {
    //   jobSpec: this.spec.name,
    //   jobId: this.jobId,
    //   key,
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
