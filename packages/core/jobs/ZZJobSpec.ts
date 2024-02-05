import {
  SpecNode,
  StreamNode,
  getNodesConnectedToStream,
} from "../orchestrations/Graph";
import { ZZWorkerDefParams, ZZWorkerDef } from "./ZZWorker";
import { InferStreamSetType } from "@livestack/shared/StreamDefSet";
import { JobsOptions, Queue, WorkerOptions } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  GenericRecordType,
  QueueName,
  RawQueueJobData,
} from "../orchestrations/workerCommon";
import Redis from "ioredis";
import _ from "lodash";
import {
  ensureJobAndInitStatusRec,
  ensureJobRelationRec,
  ensureJobStreamConnectorRec,
  ensureStreamRec,
  getJobDatapoints,
  getJobRec,
} from "../db/knexConn";
import { v4 } from "uuid";
import longStringTruncator from "../utils/longStringTruncator";

import { z } from "zod";
import { ZZEnv } from "./ZZEnv";
import {
  ByTagInput,
  ByTagOutput,
  WrapTerminateFalse,
  WrapTerminatorAndDataId,
  wrapStreamSubscriberWithTermination,
  wrapTerminatorAndDataId,
} from "../utils/io";
import { WithTimestamp, ZZStream, ZZStreamSubscriber } from "./ZZStream";
import pLimit from "p-limit";
import { IOSpec } from "@livestack/shared";
import { InferTMap } from "@livestack/shared/IOSpec";
import { wrapIfSingle } from "@livestack/shared/IOSpec";
import { DefGraph, InstantiatedGraph, JobNode } from "../orchestrations/Graph";
import { Observable } from "rxjs";
import { TagObj, TagMaps } from "../orchestrations/ZZWorkflow";
import { resolveInstantiatedGraph } from "./resolveInstantiatedGraph";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

const queueMapByProjectIdAndQueueName = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof getCachedQueueByName>
>();

// export type CheckTMap<T> = T extends Record<string, infer V> ? T : never;

export type CheckSpec<SP> = SP extends ZZJobSpec<
  infer P,
  infer I,
  infer O,
  infer IMap,
  infer OMap
>
  ? ZZJobSpec<P, I, O, IMap, OMap>
  : never;

export type InferOutputType<
  Spec,
  K,
  OMap = InferStreamSetType<CheckSpec<Spec>["outputDefSet"]>
> = OMap[K extends keyof OMap ? K : never] | null;

export type InferInputType<
  Spec,
  K,
  IMap = InferStreamSetType<CheckSpec<Spec>["inputDefSet"]>
> = IMap[K extends keyof IMap ? K : never] | null;

export class ZZJobSpec<
  P = {},
  I = never,
  O = never,
  IMap = InferTMap<I>,
  OMap = InferTMap<O>
> extends IOSpec<I, O, IMap, OMap> {
  private readonly _zzEnv: ZZEnv | null = null;
  protected static _registryBySpecName: Record<
    string,
    ZZJobSpec<any, any, any, any, any>
  > = {};

  protected logger: ReturnType<typeof getLogger>;
  private _defGraph: DefGraph | null = null;

  public getDefGraph() {
    if (!this._defGraph) {
      this._defGraph = new DefGraph({ root: this });
    }
    return this._defGraph;
  }
  readonly jobOptions: z.ZodType<P>;

  constructor({
    zzEnv,
    name,
    jobOptions,
    output,
    input,
  }: {
    name: string;
    jobOptions?: z.ZodType<P>;
    zzEnv?: ZZEnv;
    concurrency?: number;
    input?: I;
    output?: O;
  }) {
    super({
      name,
      input: wrapIfSingle(input),
      output: wrapIfSingle(output),
    });

    this.jobOptions = jobOptions || (z.object({}) as unknown as z.ZodType<P>);
    this._zzEnv = zzEnv || null;
    this.logger = getLogger(`spec:${this.name}`);
    // if (!output) {
    //   this.logger.warn(`No output defined for job spec ${this.name}.`);
    // }
    ZZJobSpec._registryBySpecName[this.name] = this;
  }

  public static lookupByName(specName: string) {
    if (!ZZJobSpec._registryBySpecName[specName]) {
      throw new Error(`JobSpec ${specName} not defined on this machine.`);
    }
    return ZZJobSpec._registryBySpecName[specName];
  }

  public derive<NewP, NewIMap, NewOMap>(
    newP: Partial<
      ConstructorParameters<typeof ZZJobSpec<NewP, NewIMap, NewOMap>>[0]
    > & {
      name: string;
    }
  ) {
    if (newP.name === this.name) {
      throw new Error(
        `Derived job spec must have a different name from the original job spec ${this.name}.`
      );
    }
    return new ZZJobSpec<P & NewP, IMap & NewIMap, OMap & NewOMap>({
      ...newP,
      name: newP.name,
    } as ConstructorParameters<typeof ZZJobSpec<P & NewP, IMap & NewIMap, OMap & NewOMap>>[0]);
  }

  public get zzEnv() {
    const resolved = this._zzEnv || ZZEnv.global();
    return resolved;
  }

  public get zzEnvEnsured() {
    if (!this.zzEnv) {
      throw new Error(
        `ZZEnv is not configured in ZZJobSpec ${this.name}. \nPlease either pass it when constructing ZZJobSpec or set it globally using ZZEnv.setGlobal().`
      );
    }
    return this.zzEnv;
  }

  private get _rawQueue() {
    return getCachedQueueByName<P, void>({
      queueNameOnly: this.name,
      queueOptions: {
        connection: this.zzEnvEnsured.redisConfig,
      },
      projectId: this.zzEnvEnsured.projectId,
    });
  }

  // public async getJob(jobId: string) {
  //   const j = await this._rawQueue.getJob(jobId);
  //   return j || null;
  // }

  public async getJobRec(jobId: string) {
    if (!this.zzEnvEnsured.db) {
      throw new Error(
        "Database is not configured in ZZEnv, therefore can not get job record."
      );
    }
    return await getJobRec({
      specName: this.name,
      projectId: this.zzEnvEnsured.projectId,
      jobId,
      dbConn: this.zzEnvEnsured.db,
    });
  }

  public async getJobStatus(jobId: string) {
    const job = await this.getJobRec(jobId);
    return job?.status || null;
  }

  public async getJobData<
    T extends "in" | "out",
    K extends T extends "in" ? keyof IMap : keyof OMap,
    U = T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
  >({
    jobId,
    ioType = "out" as T,
    order,
    limit,
    key,
  }: {
    jobId: string;
    ioType?: T;
    order?: "asc" | "desc";
    limit?: number;
    key?: K;
  }) {
    if (!this.zzEnvEnsured.db) {
      throw new Error(
        "Database is not configured in ZZEnv, therefore can not get job data."
      );
    }
    if (!key) {
      if (ioType === "in") {
        key = this.getSingleInputTag() as typeof key;
      } else {
        key = this.getSingleOutputTag() as typeof key;
      }
    }
    const recs = await getJobDatapoints<
      WithTimestamp<WrapTerminatorAndDataId<U>>
    >({
      specName: this.name,
      projectId: this.zzEnvEnsured.projectId,
      jobId,
      dbConn: this.zzEnvEnsured.db,
      ioType,
      order,
      limit,
      key: String(key),
    });
    const points = recs
      .map((r) => r.data)
      .filter((r) => r.terminate === false)
      .map((r) => ({
        data: (r as WithTimestamp<WrapTerminateFalse<U>>).data as U,
        timestamp: r.timestamp,
      }));
    return points;
  }

  public async enqueueJobAndWaitOnSingleResults<K extends keyof OMap>({
    jobId: jobId,
    jobOptions,
    outputTag,
  }: // queueEventsOptions,
  {
    jobId?: string;
    jobOptions?: P;
    outputTag?: K;
  }): Promise<{ data: OMap[K]; timestamp: number } | null> {
    if (!jobId) {
      jobId = `${this.name}-${v4()}`;
    }

    if (!outputTag) {
      outputTag = this.getSingleOutputTag() as K;
    }

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(jobOptions)}.`
    );

    const { output, input } = await this.enqueueJob({
      jobId,
      jobOptions,
    });

    const r = await output(outputTag).nextValue();
    input.tags.forEach((k) => {
      input.terminate(k);
    });
    return r;
  }

  async getJobManager(jobId: string) {
    const input = this._deriveInputsForJob(jobId);
    const output = this._deriveOutputsForJob(jobId);

    const instaG = await resolveInstantiatedGraph({
      spec: this,
      jobId,
      zzEnv: this.zzEnvEnsured,
    });

    return new JobManager<P, I, O, IMap, OMap>({
      spec: this,
      jobId,
      input,
      output,
      instantiatedGraph: instaG,
    });
  }

  async feedJobInput<K extends keyof IMap>({
    jobId,
    data,
    tag = this.getSingleInputTag() as K,
  }: {
    jobId: string;
    data: IMap[K];
    tag?: K;
  }) {
    return this._deriveInputsForJob(jobId)(tag).feed(data);
  }

  private _sendFnsByJobIdAndKey: {
    [jobId: string]: Partial<{
      in: Partial<{
        [key in keyof IMap]: ReturnType<typeof pLimit>;
      }>;
      out: Partial<{
        [key in keyof OMap]: ReturnType<typeof pLimit>;
      }>;
    }>;
  } = {};

  // p-limit feeding to guarantee the order of data sent to the stream
  public async _getStreamAndSendDataToPLimited<T extends "in" | "out">({
    jobId,
    tag,
    type,
    data: d,
  }: {
    jobId: string;
    tag: T extends "in" ? keyof IMap : keyof OMap;
    type: T;
    data: WrapTerminatorAndDataId<
      T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
    >;
  }) {
    if (!this._sendFnsByJobIdAndKey[jobId]) {
      this._sendFnsByJobIdAndKey[jobId] = {};
    }
    if (!this._sendFnsByJobIdAndKey[jobId][type]) {
      this._sendFnsByJobIdAndKey[jobId][type] = {};
    }
    const dict = this._sendFnsByJobIdAndKey[jobId][type]! as Partial<{
      [key in T extends "in" ? keyof IMap : keyof OMap]: ReturnType<
        typeof pLimit
      >;
    }>;

    if (!dict[tag]) {
      dict[tag] = pLimit(1);
    }

    const limit = dict[tag]!;

    await limit(async () => {
      // console.debug("stream_data", JSON.stringify(d, longStringTruncator));

      const redis = new Redis(this.zzEnvEnsured.redisConfig);
      if (!d.terminate) {
        // const lastV = await stream.lastValue();
        // mark job terminated in redis key value store
        const isTerminated =
          (await redis.get(`terminated__${jobId}/${type}/${String(tag)}`)) ===
          "true";
        await redis.disconnect();
        if (isTerminated) {
          this.logger.error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream! jobId: ${jobId}, tag: ${String(tag)}`
          );
          throw new Error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream!`
          );
        }
      } else {
        await redis.set(
          `terminated__${jobId}/${type}/${String(tag)}`,
          "true",
          "EX",
          600
        );
      }
      await redis.disconnect();

      const stream = await this.getJobStream({
        jobId,
        tag: tag,
        type,
      });
      // console.debug(
      //   "stream_data",
      //   stream.uniqueName,
      //   JSON.stringify(d, longStringTruncator)
      // );
      await stream.pub({
        message: d,
        ...(type === "out"
          ? {
              jobInfo: {
                jobId,
                jobOutputKey: String(tag),
              },
            }
          : {}),
      });
      return {
        streamId: stream.uniqueName,
      };
    });
  }

  async terminateJobInput({ jobId, tag }: { jobId: string; tag?: keyof IMap }) {
    return this._deriveInputsForJob(jobId)(tag).terminate();
  }

  private streamIdOverridesByTagByTypeByJobId: {
    [jobId: string]: {
      in: Partial<Record<keyof IMap, string>> | null;
      out: Partial<Record<keyof OMap, string>> | null;
    };
  } = {};

  protected async getStreamIdForJob({
    jobId,
    type,
    p,
  }: {
    jobId: string;
    type: "in" | "out";
    p: {
      tag: keyof IMap | keyof OMap;
    };
  }) {
    const instaG = await resolveInstantiatedGraph({
      spec: this,
      jobId,
      zzEnv: this.zzEnvEnsured,
    });

    const streamNodeId = instaG.findNode((nId) => {
      const n = instaG.getNodeAttributes(nId);
      if (n.nodeType !== "stream") {
        return false;
      } else {
        const { source, targets } = getNodesConnectedToStream(instaG, nId);

        if (type === "out") {
          return (
            (source &&
              source.origin.jobId === jobId &&
              source.outletNode.tag === p.tag) ||
            false
          );
        } else if (type === "in") {
          return targets.some(
            (t) => t.destination.jobId === jobId && t.inletNode.tag === p.tag
          );
        }
      }
    });

    if (!streamNodeId) {
      throw new Error(
        `Stream node not found for job ${jobId} and tag ${p.tag.toString()}`
      );
    }
    const streamNode = instaG.getNodeAttributes(streamNodeId) as StreamNode;

    return streamNode.streamId;
  }

  public getInputJobStream = async <K extends keyof IMap>(p: {
    jobId: string;
    tag: K;
  }) => {
    return (await this.getJobStream({
      ...p,
      type: "in",
    })) as ZZStream<WrapTerminatorAndDataId<IMap[K]>>;
  };

  public getOutputJobStream = async <K extends keyof OMap>(p: {
    jobId: string;
    tag: K;
  }) => {
    return (await this.getJobStream({
      ...p,
      type: "out",
    })) as ZZStream<WrapTerminatorAndDataId<OMap[K]>>;
  };

  protected getJobStream = async <
    TT extends "in" | "out",
    K extends TT extends "in" ? keyof IMap : keyof OMap
  >(p: {
    jobId: string;
    type: TT;
    tag: K;
  }) => {
    const { jobId, type } = p;
    type T = TT extends "in" ? IMap[keyof IMap] : OMap[keyof OMap];

    const specTagInfo = this.convertWorkflowAliasToSpecTag({
      alias: p.tag,
      type,
    });

    const responsibleSpec = ZZJobSpec.lookupByName(specTagInfo.specName);

    let def: z.ZodType<WrapTerminatorAndDataId<T>>;
    if (type === "in") {
      if (!responsibleSpec.inputDefSet) {
        throw new Error(
          `No input defined for job spec ${responsibleSpec.name}`
        );
      }
      def = wrapTerminatorAndDataId(
        responsibleSpec.inputDefSet.getDef(
          (specTagInfo.tag || responsibleSpec.getSingleInputTag()) as keyof IMap
        )
      ) as z.ZodType<WrapTerminatorAndDataId<T>>;
    } else if (type === "out") {
      def = wrapTerminatorAndDataId(
        responsibleSpec.outputDefSet.getDef(
          (specTagInfo.tag ||
            responsibleSpec.getSingleOutputTag()) as keyof OMap
        )
      ) as z.ZodType<WrapTerminatorAndDataId<T>>;
    } else {
      throw new Error(`Invalid type ${type}`);
    }
    const responsibleJobId = await this.lookUpChildJobIdByGroupIDAndSpecTag({
      groupId: jobId,
      specInfo: specTagInfo,
    });

    const streamId = await responsibleSpec.getStreamIdForJob({
      jobId: responsibleJobId,
      type,
      p: specTagInfo,
    });

    const stream = await ZZStream.getOrCreate<WrapTerminatorAndDataId<T>>({
      uniqueName: streamId,
      def,
      logger: responsibleSpec.logger,
      zzEnv: responsibleSpec.zzEnv,
    });
    return stream as ZZStream<WrapTerminatorAndDataId<T>>;
  };

  private _outputCollectorByJobIdAndTag: {
    [k: `${string}/${string}`]: ByTagOutput<OMap[keyof OMap]>;
  } = {};

  public createOutputCollector<K extends keyof OMap>({
    jobId,
    tag,
    from = "beginning",
  }: {
    jobId: string;
    tag?: K;
    from?: "beginning" | "now";
  }): ByTagOutput<OMap[K]> {
    const tagToWatch = tag || (this.getSingleOutputTag() as K);
    const streamP = this.getOutputJobStream({
      jobId,
      tag: tagToWatch,
    });
    const subuscriberP = new Promise<
      ZZStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>
    >((resolve, reject) => {
      streamP.then((stream) => {
        // console.debug("Output collector for stream", stream.uniqueName);
        let subscriber: ZZStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>;
        if (from === "beginning") {
          subscriber = stream.subFromBeginning();
        } else if (from === "now") {
          subscriber = stream.subFromNow();
        } else {
          reject(new Error(`Invalid "from" ${from}`));
          return undefined;
        }
        resolve(subscriber);
      });
    });
    const streamIdP = streamP.then((stream) => stream.uniqueName);

    return wrapStreamSubscriberWithTermination(streamIdP, subuscriberP);
  }

  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: P;
    parentJobId?: string;
    uniqueSpecLabel?: string;
    bullMQJobsOpts?: JobsOptions;
    inputStreamIdOverridesByTag?: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByTag?: Partial<Record<keyof OMap, string>>;
  }) {
    let {
      jobId = v4(),
      jobOptions,
      bullMQJobsOpts,
      inputStreamIdOverridesByTag,
      outputStreamIdOverridesByTag,
    } = p || {};

    // console.debug("ZZJobSpec._enqueueJob", jobId, jobOptions);
    // force job id to be the same as name
    const workers = await this._rawQueue.getWorkers();
    if (workers.length === 0) {
      this.logger.warn(
        `No worker for queue ${this._rawQueue.name}; job ${jobId} might be be stuck.`
      );
    }

    const projectId = this.zzEnvEnsured.projectId;

    await this.zzEnvEnsured.db.transaction(async (trx) => {
      await ensureJobAndInitStatusRec({
        projectId,
        specName: this.name,
        jobId,
        dbConn: trx,
        jobOptions,
      });

      if (p?.parentJobId) {
        await ensureJobRelationRec({
          projectId: this.zzEnvEnsured.projectId,
          parentJobId: p.parentJobId,
          childJobId: jobId,
          dbConn: trx,
          uniqueSpecLabel: p.uniqueSpecLabel,
        });
      }

      if (inputStreamIdOverridesByTag) {
        for (const [key, streamId] of _.entries(inputStreamIdOverridesByTag)) {
          await ensureStreamRec({
            projectId,
            streamId: streamId as string,
            dbConn: trx,
          });
          await ensureJobStreamConnectorRec({
            projectId,
            streamId: streamId as string,
            dbConn: trx,
            jobId,
            key,
            connectorType: "in",
          });
        }
        if (!this.streamIdOverridesByTagByTypeByJobId[jobId]) {
          this.streamIdOverridesByTagByTypeByJobId[jobId] = {
            in: null,
            out: null,
          };
        }
        this.streamIdOverridesByTagByTypeByJobId[jobId]["in"] =
          inputStreamIdOverridesByTag;
      }

      if (outputStreamIdOverridesByTag) {
        for (const [key, streamId] of _.entries(outputStreamIdOverridesByTag)) {
          await ensureStreamRec({
            projectId,
            streamId: streamId as string,
            dbConn: trx,
          });
          await ensureJobStreamConnectorRec({
            projectId,
            streamId: streamId as string,
            dbConn: trx,
            jobId,
            key,
            connectorType: "out",
          });
        }
        this.streamIdOverridesByTagByTypeByJobId[jobId]["out"] =
          outputStreamIdOverridesByTag;
      }
      await trx.commit();
    });
    jobOptions = jobOptions || ({} as P);
    const j = await this._rawQueue.add(
      jobId,
      { jobOptions, contextId: p?.parentJobId || null },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    this.logger.info(
      `Added job with ID ${j.id} to jobSpec: ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    return await this.getJobManager(jobId);
  }

  protected async lookUpChildJobIdByGroupIDAndSpecTag({
    groupId,
  }: {
    groupId: string;
    specInfo: {
      specName: string;
      uniqueSpecLabel?: string;
    };
  }) {
    return groupId;
  }

  public _deriveInputsForJob: (jobId: string) => JobInput<IMap> = (
    jobId: string
  ) => {
    const that = this;
    return (() => {
      const genByTagFn = () => {
        return <K extends keyof IMap>(tag?: K) => {
          const resolvedTag = tag || (that.getSingleInputTag() as K);
          return {
            feed: async (data: IMap[K]) => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = ZZJobSpec.lookupByName(
                specTagInfo.specName
              );
              const responsibleJobId =
                await this.lookUpChildJobIdByGroupIDAndSpecTag({
                  groupId: jobId,
                  specInfo: specTagInfo,
                });

              return await responsibleSpec._getStreamAndSendDataToPLimited({
                jobId: responsibleJobId,
                tag: specTagInfo.tag,
                type: "in",
                data: {
                  data,
                  terminate: false,
                },
              });
            },
            terminate: async () => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = ZZJobSpec.lookupByName(
                specTagInfo.specName
              );
              const responsibleJobId =
                await this.lookUpChildJobIdByGroupIDAndSpecTag({
                  groupId: jobId,
                  specInfo: specTagInfo,
                });

              await responsibleSpec._getStreamAndSendDataToPLimited({
                jobId: responsibleJobId,
                tag: specTagInfo.tag,
                type: "in",
                data: {
                  terminate: true,
                },
              });
            },
            getStreamId: async () => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = ZZJobSpec.lookupByName(
                specTagInfo.specName
              );

              const s = await responsibleSpec.getInputJobStream({
                jobId,
                tag: resolvedTag,
              });
              return s.uniqueName;
            },
          };
        };
      };
      const byTagFn = genByTagFn() as JobInput<IMap>;
      byTagFn.byTag = genByTagFn();
      byTagFn.tags = this.inputDefSet.keys;

      byTagFn.feed = async (data: IMap[keyof IMap]) => {
        const tag = that.getSingleInputTag();
        if (!tag) {
          throw new Error(
            `Cannot find any input to feed to for spec ${this.name}.`
          );
        }
        return await byTagFn(tag).feed(data);
      };

      byTagFn.terminate = async <K extends keyof IMap>(tag?: K) => {
        tag = tag || (that.getSingleInputTag() as K);
        return await byTagFn(tag).terminate();
      };
      return byTagFn;
    })();
  };

  public _deriveOutputsForJob: (jobId: string) => JobOutput<OMap> = (
    jobId: string
  ) => {
    const singletonSubscriberByTag = <K extends keyof OMap>(tag: K) => {
      const specTagInfo = this.convertWorkflowAliasToSpecTag({
        alias: tag,
        type: "out",
      });
      const responsibleSpec = ZZJobSpec.lookupByName(specTagInfo.specName);

      if (
        !responsibleSpec._outputCollectorByJobIdAndTag[
          `${jobId}/${tag.toString()}`
        ]
      ) {
        const subscriber = responsibleSpec.createOutputCollector({
          jobId,
          tag: specTagInfo.tag,
        });
        responsibleSpec._outputCollectorByJobIdAndTag[
          `${jobId}/${specTagInfo.tag.toString()}`
        ] = subscriber;
      }

      const subscriber = responsibleSpec._outputCollectorByJobIdAndTag[
        `${jobId}/${specTagInfo.tag.toString()}`
      ] as ByTagOutput<OMap[K]>;

      return {
        getStreamId: subscriber.getStreamId,
        nextValue: subscriber.nextValue,
        valueObservable: subscriber.valueObservable,
        async *[Symbol.asyncIterator]() {
          while (true) {
            const input = await subscriber.nextValue();

            // Assuming nextInput returns null or a similar value to indicate completion
            if (!input) {
              break;
            }
            yield input;
          }
        },
      };
    };

    let subscriberByDefaultTag: Awaited<
      ReturnType<typeof singletonSubscriberByTag>
    > | null = null;

    const nextValue = async <K extends keyof OMap>() => {
      if (subscriberByDefaultTag === null) {
        const tag = this.getSingleOutputTag() as K;
        subscriberByDefaultTag = await singletonSubscriberByTag(tag);
      }
      return await subscriberByDefaultTag.nextValue();
    };

    return (() => {
      const func = (<K extends keyof OMap>(tag?: K) => {
        if (!tag) {
          tag = this.getSingleOutputTag() as K;
        }
        return singletonSubscriberByTag(tag);
      }) as JobOutput<OMap>;
      func.byTag = singletonSubscriberByTag;
      func.tags = this.outputDefSet.keys;
      func.nextValue = nextValue;
      (func.valueObservable = new Observable<{
        data: OMap[keyof OMap];
        timestamp: number;
      }>((subscriber) => {
        while (true) {
          const next = nextValue();
          if (!next) {
            break;
          }
          subscriber.complete();
        }
      })),
        (func[Symbol.asyncIterator] = async function* () {
          while (true) {
            const input = await nextValue();

            // Assuming nextInput returns null or a similar value to indicate completion
            if (!input) {
              break;
            }
            yield input;
          }
        });
      return func;
    })();
  };

  public getSingleInputTag() {
    return this.getSingleTag("input");
  }
  public getSingleOutputTag() {
    return this.getSingleTag("output");
  }

  protected convertSpecTagToWorkflowAlias({
    tag,
  }: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
  }) {
    return tag;
  }

  protected convertWorkflowAliasToSpecTag({
    type,
    alias,
  }: {
    type: "in" | "out";
    alias: string | symbol | number;
  }): {
    specName: string;
    tag: string;
    type: "in" | "out";
    uniqueSpecLabel?: string;
  } {
    return {
      specName: this.name,
      tag: alias.toString(),
      type,
      uniqueSpecLabel: undefined as string | undefined,
    };
  }

  private getSingleTag<T extends "input" | "output">(
    type: T
  ): T extends "input" ? keyof IMap : keyof OMap {
    // naive implementation: find spec node with only one input
    // or output which is not connected to another spec, and return its tag
    const dg = this.getDefGraph();
    let specNodeIds = dg.getSpecNodeIds();
    if (specNodeIds.length === 0) {
      const rootSpecNodeId = dg.getRootSpecNodeId();
      specNodeIds = [rootSpecNodeId];
    } else {
      specNodeIds = dg.getSpecNodeIds();
    }

    const pass0 = specNodeIds.map((specNodeId) => {
      const specNode = dg.getNodeAttributes(specNodeId) as SpecNode;
      if (type === "input") {
        return {
          conns: dg.getInboundNodeSets(specNodeId).map((s) => ({
            specName: specNode.specName,
            uniqueSpecLabel: specNode.uniqueSpecLabel,
            type: "in" as const,
            tag: s.inletNode.tag,
            inletNodeId: s.inletNode.id,
            streamNodeId: s.streamNode.id,
          })),
          specNodeId,
        };
      } else {
        return {
          conns: dg.getOutboundNodeSets(specNodeId).map((s) => ({
            specName: specNode.specName,
            uniqueSpecLabel: specNode.uniqueSpecLabel,
            type: "out" as const,
            tag: s.outletNode.tag,
            outletNodeId: s.outletNode.id,
            streamNodeId: s.streamNode.id,
          })),
          specNodeId,
        };
      }
    });
    const pass1 = pass0.filter(({ conns }) => conns.length === 1);
    const pass2 = pass1
      .filter(({ conns }) => {
        const conn = conns[0];
        if (type === "input") {
          const { source } = getNodesConnectedToStream(dg, conn.streamNodeId);
          return !source;
        } else {
          const { targets } = getNodesConnectedToStream(dg, conn.streamNodeId);
          return targets.length === 0;
        }
      })
      .map((qualified) => ({
        ...qualified,
        conns: qualified.conns.map((c) => ({
          ...c,
          alias: this.convertSpecTagToWorkflowAlias(c),
        })),
      }));
    const qualified = pass2;
    if (qualified.length === 0) {
      throw new Error(
        `Cannot find a single unambiguous ${type} for spec "${
          this.name
        }". Please specify at least one in the "${type}" field of the spec's definition. Available ${type}s: ${
          qualified.map((q) => q.conns[0].tag).join(", ") || "none"
        }`
      );
    } else if (qualified.length > 1) {
      throw new Error(
        `Ambiguous ${type} for spec "${this.name}"; found more than two child specs with a single ${type}. \nPlease specify which one to use with "${type}(tagName)".`
      );
    } else {
      const qualifiedC = qualified[0].conns[0];
      if (!qualifiedC.alias) {
        // the user didn't give a public tag; auto-register
      }
      const t =
        (qualified[0].conns[0].alias as T extends "input"
          ? keyof IMap
          : keyof OMap) || null;

      if (!t) {
        throw new Error(
          `Failed to find single and only tag on ${type} of spec ${this.name}.`
        );
      }
      return t;
    }
  }

  // public jobIdBySpec(groupId: string, spec: UniqueSpecQuery): string {
  //   const { jobId } = this.identifySpecAndJobIdBySpecQuery(groupId, spec);
  //   return jobId;
  // }

  // private identifySpecAndJobIdBySpecQuery(
  //   groupId: string,
  //   specQuery: UniqueSpecQuery
  // ): {
  //   spec: ZZJobSpec<any, any, any>;
  //   jobId: string;
  // } {
  //   const specInfo = resolveUniqueSpec(specQuery);
  //   const jobNodeId = this.graph.findNode((id, n) => {
  //     return (
  //       n.nodeType === "job" &&
  //       n.specName === specInfo.spec.name &&
  //       n.uniqueSpecLabel === specInfo.uniqueSpecLabel
  //     );
  //   });

  //   if (!jobNodeId) {
  //     throw new Error(
  //       `Job of spec ${specInfo.spec.name} ${
  //         specInfo.uniqueSpecLabel
  //           ? `with label ${specInfo.uniqueSpecLabel}`
  //           : ""
  //       } not found.`
  //     );
  //   }
  //   const jobId = (this.graph.getNodeAttributes(jobNodeId) as JobNode).jobId;
  //   const childSpec = ZZJobSpec.lookupByName(specInfo.spec.name);

  //   return { spec: childSpec, jobId };
  // }

  // toString
  public toString() {
    return this.name;
  }

  toJSON() {
    return this.name;
  }

  // convenient function
  public defineWorker<WP extends object>(
    p: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec">
  ) {
    return new ZZWorkerDef<P, I, O, WP, IMap, OMap>({
      ...p,
      jobSpec: this,
    });
  }

  public async defineWorkerAndStart<WP extends object>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }

  public static override define<P, I, O>(
    p: ConstructorParameters<typeof ZZJobSpec<P, I, O>>[0]
  ) {
    return new ZZJobSpec<P, I, O>(p);
  }
}

export const getCachedQueueByName = <JobOptions, JobReturnType>(
  p: {
    projectId: string;
    queueNameOnly: string;
    queueOptions?: WorkerOptions;
  }
  // & {
  //   queueNamesDef: T;
  // }
) => {
  const {
    // queueNamesDef,
    queueNameOnly,
    projectId,
    queueOptions,
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMapByProjectIdAndQueueName.get(
    `${projectId}/${queueNameOnly}`
  ) as Queue<RawQueueJobData<JobOptions>, JobReturnType>;
  if (existing) {
    return existing;
  } else {
    const queue = new Queue<RawQueueJobData<JobOptions>, JobReturnType>(
      `${projectId}/${queueNameOnly}`,
      queueOptions
    );
    queueMapByProjectIdAndQueueName.set(`${projectId}/${queueNameOnly}`, queue);
    return queue;
  }
};
export class JobManager<P, I, O, IMap, OMap> {
  public readonly input: JobInput<IMap>;
  public readonly output: JobOutput<OMap>;
  public readonly jobId: string;
  public readonly graph: InstantiatedGraph;

  constructor({
    spec,
    jobId,
    input,
    output,
    instantiatedGraph,
  }: {
    spec: ZZJobSpec<P, I, O, IMap, OMap>;
    jobId: string;
    input: JobInput<IMap>;
    output: JobOutput<OMap>;
    instantiatedGraph: InstantiatedGraph;
  }) {
    // TODO
    this.graph = instantiatedGraph;
    this.input = input;
    this.output = output;
    this.jobId = jobId;
  }
}

export interface JobInput<IMap> {
  <K extends keyof IMap>(tag?: K): ByTagInput<IMap[K]>;
  tags: (keyof IMap)[];
  feed: <K extends keyof IMap>(data: IMap[K], tag?: K) => Promise<void>;
  terminate: <K extends keyof IMap>(tag?: K) => Promise<void>;
  byTag: <K extends keyof IMap>(tag: K) => ByTagInput<IMap[K]>;
}

export interface JobOutput<OMap> {
  <K extends keyof OMap>(tag?: K): ByTagOutput<OMap[K]>;
  tags: (keyof OMap)[];
  byTag: <K extends keyof OMap>(tag: K) => ByTagOutput<OMap[K]>;
  nextValue: () => Promise<{
    data: OMap[keyof OMap];
    timestamp: number;
  } | null>;
  valueObservable: Observable<{
    data: OMap[keyof OMap];
    timestamp: number;
  } | null>;

  [Symbol.asyncIterator]: () => AsyncIterableIterator<{
    data: OMap[keyof OMap];
    timestamp: number;
  }>;
}
export const SpecOrName = z.union([
  z.string(),
  z.instanceof(ZZJobSpec<any, any, any, any, any>),
]);
export type SpecOrName = z.infer<typeof SpecOrName>; // Conversion functions using TypeScript

export const UniqueSpecQuery = z.union([
  SpecOrName,
  z.object({
    spec: SpecOrName,
    label: z.string().default("default_label"),
  }),
]);

export type UniqueSpecQuery = z.infer<typeof UniqueSpecQuery>;
export const SpecAndOutlet = z.union([
  UniqueSpecQuery,
  z.tuple([UniqueSpecQuery, z.string().or(z.literal("default"))]),
]);
export type SpecAndOutlet = z.infer<typeof SpecAndOutlet>;
export function resolveUniqueSpec(
  uniqueSpec: UniqueSpecQuery | TagObj<any, any, any, any, any>
): {
  spec: ZZJobSpec<any, any, any>;
  uniqueSpecLabel?: string;
} {
  if (typeof uniqueSpec === "string") {
    const spec = ZZJobSpec.lookupByName(uniqueSpec);
    return {
      spec,
    };
  } else if ("spec" in (uniqueSpec as any) && "label" in (uniqueSpec as any)) {
    const spec = convertSpecOrName(
      (
        uniqueSpec as {
          spec: SpecOrName;
          label: string;
        }
      ).spec
    );

    return {
      spec,
      uniqueSpecLabel: (
        uniqueSpec as {
          spec: SpecOrName;
          label: string;
        }
      ).label,
    };
  } else {
    const spec = convertSpecOrName(uniqueSpec as any);
    return {
      spec,
    };
  }
}

export function convertSpecOrName(
  specOrName: SpecOrName | TagObj<any, any, any, any, any>
) {
  if (typeof specOrName === "string") {
    return ZZJobSpec.lookupByName(specOrName);
  } else if (specOrName instanceof ZZJobSpec) {
    return specOrName;
  } else if (specOrName.spec instanceof ZZJobSpec) {
    return convertSpecOrName(specOrName.spec);
  } else {
    throw new Error("Invalid spec");
  }
}
export function resolveTagMapping({
  _aliasMaps,
}:
  | {
      _aliasMaps?: Partial<TagMaps<any, any, any, any>>;
    }
  | any) {
  return {
    inputAliasMap: _aliasMaps?.inputTag ?? {},
    outputAliasMap: _aliasMaps?.outputTag ?? {},
  };
}
