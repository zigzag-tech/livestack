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
import _ from "lodash";

export type ZZProcessor<PP, WP extends object = {}> = PP extends ZZJobSpec<
  infer P,
  infer IMap,
  infer OMap
>
  ? (j: ZZJob<P, IMap, OMap, WP>) => Promise<OMap[keyof OMap] | void>
  : never;

export class ZZJob<P, IMap, OMap, WP extends object = {}> {
  private readonly bullMQJob: Job<{ jobParams: P }, void>;
  public readonly _bullMQToken?: string;
  readonly jobParams: P;
  readonly logger: ReturnType<typeof getLogger>;
  readonly spec: ZZJobSpec<P, IMap, OMap>;
  // readonly nextInput: <K extends keyof IMap>(
  //   key?: K
  // ) => Promise<IMap[K] | null>;

  //async iterator
  readonly input: ReturnType<typeof this.genInputObject> & {
    byKey: <K extends keyof IMap>(
      key: K
    ) => ReturnType<ZZJob<P, IMap, OMap, WP>["genInputObject"]>;
  };

  // New properties for subscriber tracking

  readonly output: { 
    emit: (o: OMap[keyof OMap]) => Promise<void> ;
    byKey: <K extends keyof OMap>(
      key: K
    ) => {
      emit: (o: OMap[K]) => Promise<void>;
    }
  };

  storageProvider?: IStorageProvider;
  readonly zzEnv: ZZEnv;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  public jobId: string;
  private readonly workerName;
  private readonly inputStreamFnsByKey: Partial<{
    [K in keyof IMap]: {
      nextValue: () => Promise<IMap[K] | null>;
      // inputStream: ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
      inputObservableUntracked: Observable<IMap[K] | null>;
      trackedObservable: Observable<IMap[K] | null>;
      subscriberCountObservable: Observable<number>;
      loopUntilInputTerminated: (
        processor: (input: IMap[K]) => Promise<void>
      ) => Promise<void>;
    };
  }>;

  constructor(p: {
    bullMQJob: Job<{ jobParams: P }, void, string>;
    bullMQToken?: string;
    logger: ReturnType<typeof getLogger>;
    jobParams: P;
    storageProvider?: IStorageProvider;
    jobSpec: ZZJobSpec<P, IMap, OMap>;
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
      this.jobParams = p.jobSpec.jobParams.parse(p.jobParams) as P;
    } catch (err) {
      this.logger.error(
        `jobParams error: jobParams provided is invalid: ${JSON.stringify(
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
    this.zzEnv = p.jobSpec.zzEnv;
    this.spec = p.jobSpec;

    this.inputStreamFnsByKey = {};

    const reportOnReady = (
      obs: Observable<IMap[keyof IMap] | null>,
      key: keyof IMap | "default"
    ) => {
      const sub = obs.subscribe(async () => {
        await this.setJobReadyForInputsInRedis({
          redisConfig: this.zzEnv.redisConfig,
          jobId: this.jobId,
          isReady: true,
          key: key ? key : "default",
        });
        sub.unsubscribe();
      });
    };

    this.input = {
      ...this.genInputObject("default" as keyof IMap),
      byKey: (key: keyof IMap) => {
        const obj = this.genInputObject(key);
        reportOnReady(obj.observable, key);
        return obj;
      },
    };
    reportOnReady(this.input.observable, "default");

    const emitOutput = async <K extends keyof OMap>(
      o: OMap[K],
      key: K = "default" as K
    ) => {
      // this.logger.info(
      //   `Emitting output: ${this.jobId}, ${this.def.name} ` +
      //     JSON.stringify(o, longStringTruncator)
      // );

      await this.spec._getStreamAndSendDataToPLimited({
        jobId: this.jobId,
        type: "out",
        key,
        data: {
          data: o,
          terminate: false,
        },
      });

      this.bullMQJob.updateProgress(this._dummyProgressCount++);
    };
    this.output = {
      emit: emitOutput,
      byKey: (key: keyof OMap) => ({
        emit: (o: OMap[typeof key]) => emitOutput(o, key),
      }),
    };
  }

  private readonly genInputObject = <K extends keyof IMap>(key: K) => {
    const job = this;
    const nextValue = async <K extends keyof IMap>(key?: K) => {
      const r = await (await job._ensureInputStreamFn(key)).nextValue();
      return r as IMap[K] | null;
    };
    return {
      nextValue,
      get observable() {
        return job._ensureInputStreamFn(key).trackedObservable;
      },
      async *[Symbol.asyncIterator]() {
        while (true) {
          const input = await nextValue(key);

          // Assuming nextInput returns null or a similar value to indicate completion
          if (!input) {
            break;
          }
          yield input;
        }
      },
    };
  };

  private _ensureInputStreamFn<K extends keyof IMap>(key?: K | "default") {
    if (this.spec.inputDefSet.isSingle) {
      if (key && key !== "default") {
        throw new Error(
          `inputDefs is single stream, but key is provided: ${String(key)}`
        );
      }
      key = "default" as const;
    } else {
      if (!key) {
        throw new Error(
          `inputDefs is multiple streams, but key is not provided`
        );
      }
    }

    if (!this.inputStreamFnsByKey[key! as keyof IMap]) {
      const streamP = this.spec.getJobStream({
        jobId: this.jobId,
        type: "in",
        key: key! as keyof IMap,
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
            key: key || "default",
          });
        }
      });

      this.inputStreamFnsByKey[key as K] = {
        nextValue,
        // inputStream: stream,
        inputObservableUntracked,
        trackedObservable,
        subscriberCountObservable,
        loopUntilInputTerminated: genLoopUntilTerminated(nextValue),
      };
    }
    return this.inputStreamFnsByKey[key as K]!;
  }

  setJobReadyForInputsInRedis = async ({
    redisConfig,
    jobId,
    key,
    isReady,
  }: {
    redisConfig: RedisOptions;
    jobId: string;
    key: keyof IMap | "default";
    isReady: boolean;
  }) => {
    // console.debug("setJobReadyForInputsInRedis", {
    //   jobSpec: this.jobSpec.name,
    //   jobId,
    //   key,
    //   isReady,
    // });
    if (!key) {
      throw new Error("key is required");
    }
    try {
      const redis = new Redis(redisConfig);
      await redis.set(
        `ready_status__${jobId}/${String(key)}`,
        isReady ? "true" : "false"
      );
    } catch (error) {
      console.error("Error setJobReadyForInputsInRedis:", error);
    }
  };

  public beginProcessing = async (
    processor: ZZProcessor<ZZJobSpec<P, IMap, OMap>, WP>
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
      `Picked up job: ${job.id}, ${job.queueName} ` +
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
            this.inputStreamFnsByKey
          ) as (typeof this.inputStreamFnsByKey)[keyof IMap][]
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
      await this.signalOutputEnd();

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

  signalOutputEnd = async (key?: keyof OMap) => {
    // console.debug("signalOutputEnd", {
    //   jobSpec: this.spec.name,
    //   jobId: this.jobId,
    //   key,
    // });
    const outputStream = await this.spec.getJobStream({
      jobId: this.jobId,
      type: "out",
      key: key || ("default" as keyof OMap),
    });
    await outputStream.pub({
      message: {
        terminate: true,
      },
    });
  };

  public static define<P, IMap, OMap>(
    p: ConstructorParameters<typeof ZZJobSpec<P, IMap, OMap>>[0]
  ) {
    return new ZZJobSpec<P, IMap, OMap>(p);
  }

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

export function genLoopUntilTerminated<T, P extends unknown[]>(
  nextValue: (...args: P) => Promise<T | null>,
  ...args: P
) {
  return async (processor: (input: T) => Promise<void>) => {
    let v: T | null;
    while ((v = await nextValue(...args)) !== null) {
      await processor(v);
    }
  };
}
