import {
  InstantiatedGraph,
  JobId,
} from "@livestack/shared/src/graph/InstantiatedGraph";
import { InferTMap } from "@livestack/shared";
import _ from "lodash";
import {
  Observable,
  Subscription,
  map,
  merge,
  takeUntil,
  tap,
  takeWhile,
  catchError,
  finalize,
  of,
  first,
  filter,
  EMPTY,
} from "rxjs";
import { z } from "zod";
import { TransformRegistry } from "../orchestrations/TransformRegistry";
import {
  createLazyNextValueGenerator,
  createTrackedObservable,
} from "./pubsub";
import { IStorageProvider, getPublicCdnUrl } from "../storage/cloudStorage";
import { getLogger } from "../utils/createWorkerLogger";
import { WrapTerminateFalse, WrapTerminatorAndDataId } from "../utils/io";
import { longStringTruncator } from "../utils/longStringTruncator";
import { DataStream, WithTimestamp } from "../streams/DataStream";
import { DataStreamSubscriber } from "../streams/DataStreamSubscriber";
import { JobSpec } from "./JobSpec";
import { ZZEnv } from "./ZZEnv";
import { identifyLargeFilesToSave } from "../files/file-ops";
import { AuthorizedGRPCClient } from "@livestack/vault-client";
import { InferDefaultOrSingleKey, InferDefaultOrSingleValue } from "./ZZWorker";

export type ZZProcessor<P, I, O, WP extends object | undefined, IMap, OMap> = (
  j: ZZJob<P, I, O, WP, IMap, OMap>
) => Promise<OMap[InferDefaultOrSingleKey<OMap>] | void>;

export interface ByTagCallable<TMap> {
  <K extends keyof TMap>(key?: K): {
    nextValue: () => Promise<TMap[K] | null>;
    observable: () => Observable<TMap[K] | null>;
    [Symbol.asyncIterator](): AsyncGenerator<TMap[K]>;
  };
}

type SmarterNextValue<IMap, T> = T extends { tag: infer Tag }
  ? Tag extends keyof IMap
    ? { tag: Tag; data: IMap[Tag] }
    : never
  : never;

export class ZZJob<
  P,
  I,
  O,
  WP extends object | undefined = {},
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

      merge: <K extends keyof IMap>(
        ...tags: [K[]] | K[]
      ) => {
        nextValue: <K extends keyof IMap>() => Promise<SmarterNextValue<
          IMap,
          {
            tag: K;
            data: IMap[K];
          }
        > | null>;
        [Symbol.asyncIterator]<K extends keyof IMap>(): AsyncGenerator<
          SmarterNextValue<
            IMap,
            {
              tag: K;
              data: IMap[K];
            }
          >
        >;
      };
    };

  // New properties for subscriber tracking

  readonly output: {
    <K extends keyof OMap>(tag?: K): {
      emit: (
        o: OMap[K extends never ? InferDefaultOrSingleKey<OMap> : K]
      ) => Promise<void>;
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

  readonly invoke: <
    P,
    I,
    O,
    IMap,
    OMap,
    T extends keyof IMap = InferDefaultOrSingleKey<IMap>
  >(
    jobSpec: JobSpec<P, I, O, IMap, OMap>,
    inputTag: T,
    inputParams: IMap[InferDefaultOrSingleKey<IMap>],
    jobOptions?: P
  ) => Promise<OMap[InferDefaultOrSingleKey<OMap>]>;

  storageProvider?: IStorageProvider;
  readonly zzEnvP: Promise<ZZEnv>;
  private _dummyProgressCount = 0;
  public workerInstanceParams: WP extends object ? WP : null =
    null as WP extends object ? WP : null;
  public jobId: JobId;
  public readonly workerName;
  private readonly inputStreamFnsByTag: Partial<{
    [K in keyof IMap]: {
      nextValue: () => Promise<WithTimestamp<
        WrapTerminateFalse<IMap[K]>
      > | null>;
      // inputStream: DataStream<WrapTerminatorAndDataId<IMap[K]>>;
      inputObservableUntracked: Observable<WithTimestamp<
        WrapTerminateFalse<IMap[K]>
      > | null>;
      trackedObservable: Observable<WithTimestamp<
        WrapTerminateFalse<IMap[K]>
      > | null>;
      subscriberCountObservable: Observable<number>;
    };
  }>;
  private updateProgress: (count: number) => Promise<void>;
  private trackerByInputOutputTag: Record<
    keyof IMap,
    Record<keyof OMap, AssociationTracker>
  >;

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

    this.trackerByInputOutputTag = {} as any;

    for (const inputTag of this.spec.inputTags) {
      this.trackerByInputOutputTag[inputTag] = {} as any;
      for (const outputTag of this.spec.outputTags) {
        const tracker = new AssociationTracker();
        this.trackerByInputOutputTag[inputTag][outputTag] = tracker;
      }
    }

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
    this.zzEnvP = p.jobSpec.zzEnvP;
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
    const thatJob = this;

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

      func.tags = obj.tags;
      func.observable = obj.observable().pipe(
        map((x) => x || null), // Maps falsy values (like undefined) to null
        first(), // Automatically complete after the first emission
        catchError((err) => {
          // Handle any errors that may occur
          console.error("Error in observable stream:", err);
          return of(null); // Optionally continue the stream with a null value
        })
      );
      func.nextValue = obj.nextValue;

      func[Symbol.asyncIterator] = obj[Symbol.asyncIterator];

      func.merge = <K extends keyof IMap>(...tags: [K[]] | K[]) => {
        let flattened: K[];
        if (Array.isArray(tags[0])) {
          flattened = tags[0] as K[];
        } else {
          // check if every element is a string
          if (tags.every((x) => typeof x === "string")) {
            flattened = tags as K[];
          } else {
            throw new Error(
              "Invalid input to merge. Syntax: merge('tag1', 'tag2') or merge(['tag1', 'tag2'])"
            );
          }
        }
        // dedupe tags
        const deduped = Array.from(new Set(flattened));
        const obs = deduped.map((tag) => {
          return this.genInputObjectByTag(tag)
            .observable()
            .pipe(
              map((data) => {
                return {
                  tag,
                  data,
                };
              })
            );
        });
        const merged = merge(...obs);
        return createLazyNextValueGenerator(merged);
      };

      // Object.assign(func, obj);
      return func as any;
    })();

    const emitOutput = async <K extends keyof OMap>(
      o: OMap[K extends never ? InferDefaultOrSingleKey<OMap> : K],
      tag?: K
    ) => {
      // this.logger.info(
      //   `Emitting output: ${this.jobId}, ${this.def.name} ` +
      //     JSON.stringify(o, longStringTruncator)
      // );

      const resolvedTag = tag || this.spec.getSingleTag("output", true);

      const relevantDatapoints: { streamId: string; datapointId: string }[] =
        [];
      for (const itag of this.spec.inputTags) {
        const stream = await this.spec.getInputJobStream({
          jobId: this.jobId,
          tag: itag,
        });
        const streamId = stream.uniqueName;
        if (this.trackerByInputOutputTag[itag][resolvedTag]) {
          // TODO: this is hacky. this.trackerByInputOutputTag[itag][resolvedTag] is null when we are in a workflow
          relevantDatapoints.push(
            ...this.trackerByInputOutputTag[itag][resolvedTag]
              .dispense()
              .map((x) => ({ streamId, datapointId: x.datapointId }))
          );
        }
      }

      await this.spec._getStreamAndSendDataToPLimited({
        jobId: this.jobId,
        type: "out",
        tag: resolvedTag,
        data: {
          data: o,
          terminate: false,
        },
        parentDatapoints: [...relevantDatapoints],
      });

      await this.updateProgress(this._dummyProgressCount++);
    };
    const that = this;

    this.invoke = async <P, I, O, IMap, OMap, T extends keyof IMap>(
      jobSpec: JobSpec<P, I, O, IMap, OMap>,
      inputTag: T,
      inputParams: IMap[InferDefaultOrSingleKey<IMap>],
      jobOptions?: P
    ): Promise<OMap[InferDefaultOrSingleKey<OMap>]> => {
      const { input, output } = await jobSpec.enqueueJob({
        jobOptions,
      });
      input.feed(inputParams);
      const data = await output.nextValue();
      if (!data) {
        throw new Error("Output is null");
      }

      return data.data;
    };

    this.output = (() => {
      const func = <K extends keyof OMap>(tag?: K) => {
        let resolvedTag: K | InferDefaultOrSingleKey<OMap> | undefined = tag;
        return {
          emit: (
            o: OMap[K extends never ? InferDefaultOrSingleKey<OMap> : K]
          ) => emitOutput(o as OMap[any], resolvedTag),
          async getStreamId() {
            if (!resolvedTag) {
              resolvedTag = that.spec.getSingleTag(
                "output",
                true
              ) as InferDefaultOrSingleKey<OMap>;
            }
            const s = await that.spec.getOutputJobStream({
              jobId: that.jobId,
              tag: resolvedTag,
            });
            return s.uniqueName;
          },
        };
      };
      func.byTag = <K extends keyof OMap>(tag: K) => ({
        emit: (o: OMap[K]) => emitOutput(o as OMap[any], tag),
        async getStreamId() {
          const s = await that.spec.getOutputJobStream({
            jobId: that.jobId,
            tag,
          });
          return s.uniqueName;
        },
      });
      func.emit = (o: OMap[keyof OMap]) => {
        const tag = this.spec.getSingleTag("output", true);
        return emitOutput(o as OMap[any], tag as keyof OMap);
      };
      func.getStreamId = async () => {
        const tag = this.spec.getSingleTag("output", true);

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

  private readonly genInputObjectByTag = <K extends keyof IMap>(_tag?: K) => {
    const that = this;
    let resolvedTag: K | InferDefaultOrSingleKey<IMap> | undefined = _tag;
    const nextValue = async () => {
      if (!resolvedTag) {
        resolvedTag = that.spec.getSingleTag(
          "input",
          true
        ) as InferDefaultOrSingleKey<IMap>;
      }
      const r = await (
        await that._ensureInputStreamFn(resolvedTag)
      ).nextValue();
      // track
      if (r) {
        for (const otag of that.spec.outputTags) {
          that.trackerByInputOutputTag[resolvedTag][otag].intake({
            datapointId: r.datapointId,
          });
        }
      }
      return r?.data || (null as IMap[K] | null);
    };
    return {
      nextValue,
      observable() {
        if (!resolvedTag) {
          resolvedTag = that.spec.getSingleTag(
            "input",
            true
          ) as InferDefaultOrSingleKey<IMap>;
        }
        const obs = that._ensureInputStreamFn(
          resolvedTag as K
        ).trackedObservable;
        // wrap observable with tracking
        return obs.pipe(
          takeWhile((x) => !!x), // Only take values that are not falsy (similar to your if (!x))
          tap((x) => {
            // Assuming x can never be falsy here because of the takeWhile
            for (const otag of that.spec.outputTags) {
              that.trackerByInputOutputTag[resolvedTag as K][otag].intake({
                datapointId: x!.datapointId,
              });
            }
          }),
          map((x) => x!.data),
          catchError((err) => {
            // Handle any errors that occur in the above operations
            console.error("Error processing observable stream:", err);
            return of(null); // Continue the stream with a null value or use throwError to re-throw the error
          }),
          finalize(() => {
            // This will execute when the consumer unsubscribes or the stream completes naturally
            console.log("Observable stream ended or unsubscribed");
          })
        );
      },
      async *[Symbol.asyncIterator]() {
        if (!resolvedTag) {
          resolvedTag = that.spec.getSingleTag(
            "input",
            true
          ) as InferDefaultOrSingleKey<IMap>;
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
          Awaited<
            ReturnType<AuthorizedGRPCClient["db"]["getParentJobRec"]>
          >["rec"]
        >,
        "job_params_str"
      > & {
        job_params: any;
      })
    | null
    | "uninitialized" = "uninitialized";

  private getParentRec = async () => {
    if (this._parentRec === "uninitialized") {
      const { null_response, rec } = await (
        await (
          await this.zzEnvP
        ).vaultClient
      ).db.getParentJobRec({
        projectUuid: (await this.zzEnvP).projectUuid,
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

  private _ensureInputStreamFn = <K extends keyof IMap>(_tag?: K) => {
    const thatJob = this;
    let resolvedTag: K | InferDefaultOrSingleKey<IMap> | undefined = _tag;
    if (this.spec.inputTags.length === 0) {
      throw new Error("inputDefs is empty for spec " + this.spec.name);
    }
    if (this.spec.isInputSingle) {
      resolvedTag = this.spec.getSingleTag(
        "input",
        true
      ) as InferDefaultOrSingleKey<IMap>;
    } else {
      if (!resolvedTag) {
        throw new Error(
          `inputDefs consists of multiple streams ${this.spec.inputTags.join(
            ", "
          )}, but key is not provided.`
        );
      }
    }

    if (!this.inputStreamFnsByTag[resolvedTag!]) {
      const streamP = this.spec.getInputJobStream({
        jobId: this.jobId,
        tag: resolvedTag!,
      }) as Promise<DataStream<WrapTerminatorAndDataId<IMap[K] | unknown>>>;
      const parentRecP = this.getParentRec();

      const inputObservableUntracked = new Observable<WithTimestamp<
        WrapTerminateFalse<IMap[K]>
      > | null>((s) => {
        Promise.all([streamP, parentRecP]).then(([stream, parentRec]) => {
          const sub = DataStreamSubscriber.subFromBeginning(stream);
          // const obs = sub.valueObservable.pipe(
          //   map((x) => (x.terminate ? null : x.data))
          // );
          const obs = sub.valueObservable;
          const subscription = obs.subscribe((n) => {
            if (!n.terminate) {
              let r: WithTimestamp<WrapTerminateFalse<IMap[K]>> = {
                ...(n as WithTimestamp<WrapTerminateFalse<IMap[K]>>),
              };

              // find any transform function defined for this input
              // and apply it if found

              const transform = !parentRec
                ? null
                : TransformRegistry.getTransform({
                    receivingSpecName: this.spec.name,
                    tag: resolvedTag!.toString(),
                    workflowSpecName: parentRec.spec_name,
                    receivingSpecUniqueLabel:
                      parentRec.unique_spec_label || null,
                  });
              if (transform) {
                r.data = transform(r.data);
              }
              s.next(r);
            } else {
              s.next(null);
              s.complete();
            }
          });

          // Store the subscription to be able to unsubscribe later
          thatJob.storeSubscription(resolvedTag!.toString(), subscription);
        });

        return () => {
          s.unsubscribe();
          thatJob.unsubscribeFromInput(resolvedTag!.toString());
        };
      });

      const { trackedObservable, subscriberCountObservable } =
        createTrackedObservable(inputObservableUntracked);

      const { nextValue } = createLazyNextValueGenerator(trackedObservable);

      this.inputStreamFnsByTag[resolvedTag as K] = {
        nextValue,
        // inputStream: stream,
        inputObservableUntracked,
        trackedObservable,
        subscriberCountObservable,
      };
    }
    return this.inputStreamFnsByTag[resolvedTag as K]!;
  };

  // Store subscriptions to allow cleanup
  private subscriptions: Record<string, Subscription> = {};

  private storeSubscription(tag: string, subscription: Subscription) {
    this.subscriptions[tag] = subscription;
  }

  private unsubscribeFromInput(tag: string) {
    if (this.subscriptions[tag]) {
      this.subscriptions[tag].unsubscribe();
      delete this.subscriptions[tag];
    }
  }

  // Call this method to clean up all subscriptions when the job is done
  private cleanupSubscriptions() {
    Object.values(this.subscriptions).forEach((subscription) => {
      subscription.unsubscribe();
    });
    this.subscriptions = {};
  }

  public beginProcessing = async (
    processor: ZZProcessor<P, I, O, WP, IMap, OMap>
  ): Promise<void> => {
    const jId = {
      specName: this.spec.name,
      jobId: this.jobId,
      projectUuid: (await this.zzEnvP).projectUuid,
    };

    const logger = this.logger;
    const projectUuid = (await this.zzEnvP).projectUuid;

    logger.info(
      `Job started. Job ID: ${this.jobId}.` +
        `${JSON.stringify(this.jobOptions, longStringTruncator)}`
    );

    try {
      await (
        await (
          await this.zzEnvP
        ).vaultClient
      ).db.appendJobStatusRec({
        ...jId,
        jobStatus: "running",
      });
      const unsubs = Promise.all(
        (
          _.values(
            this.inputStreamFnsByTag
          ) as (typeof this.inputStreamFnsByTag)[keyof IMap][]
        ).map((x) => {
          return new Promise<void>((resolve, reject) => {
            x!.subscriberCountObservable
              .pipe(
                takeUntil(x!.inputObservableUntracked),
                filter((count) => count === 0), // Only proceed when count is zero
                first(), // Ensure we only resolve once, the first time count reaches zero
                catchError((err) => {
                  console.error("Error in subscriber count tracking", err);
                  reject(err); // Properly handle and forward errors
                  return EMPTY; // Prevent further emissions and complete the stream
                }),
                finalize(() => resolve()) // Resolve the promise when the stream completes
              )
              .subscribe();
          });
        })
      );

      const processedR = await processor(this);
      // console.debug("processed", this.jobId);

      // wait as long as there are still subscribers
      await unsubs;
      if (processedR && this.spec.isOutputSingle) {
        await this.output.emit(processedR);
      }

      await (
        await (
          await this.zzEnvP
        ).vaultClient
      ).db.appendJobStatusRec({
        ...jId,
        jobStatus: "completed",
      });

      // await job.updateProgress(processedR as object);
      // console.debug("signalOutputEnd", this.jobId);
      for (const tag of Object.keys(this.spec.output) as (keyof OMap)[]) {
        // console.debug(
        //   `${this.spec.name}: Signaling output end for tag ${tag.toString()}`
        // );
        await this.signalOutputEnd(tag);
      }

      // if (processedR) {
      //   return processedR;
      // }
    } catch (e: any) {
      console.error("Error while running job: ", this.jobId, e);

      await (
        await this.zzEnvP
      ).vaultClient.db.appendJobStatusRec({
        projectUuid,
        specName: this.spec.name,
        jobId: this.jobId,
        jobStatus: "failed",
      });
      // throw e;
      // }
    } finally {
      this.cleanupSubscriptions();
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
      tag: tag || this.spec.getSingleTag("output", true),
    });
    await outputStream.pub({
      message: {
        terminate: true,
      },
      parentDatapoints: [],
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
    const { largeFilesToSave } = await identifyLargeFilesToSave(obj);
    const found = largeFilesToSave.find((x) => x.path === key);
    if (!found) {
      console.error("Available keys: ", Object.keys(obj));
      throw new Error(`Cannot find ${String(key)} in largeFilesToSave`);
    } else {
      return getPublicCdnUrl({
        projectUuid: (await this.zzEnvP).projectUuid,
        jobId: this.jobId,
        key: String(key),
        storageProvider: this.storageProvider,
      });
    }
  };
}

class AssociationTracker {
  private _currentDispensed = false;
  private _store: {
    datapointId: string;
  }[] = [];

  private _prevStore: {
    datapointId: string;
  }[] = [];

  intake = (x: { datapointId: string }) => {
    if (this._currentDispensed && this._store.length > 0) {
      this._store = [];
      this._currentDispensed = false;
    }
    this._store.push(x);
  };

  dispense = () => {
    if (this._currentDispensed && this._store.length === 0) {
      return [...this._prevStore];
    } else {
      this._currentDispensed = true;
      this._prevStore = [...this._store];
      const r = [...this._store];
      this._store = [];
      return r;
    }
  };
}