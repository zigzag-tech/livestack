import {
  InferStreamSetType,
  IOSpec,
  InferTMap,
  wrapIfSingle,
  initDefGraph,
  OutletNode,
} from "@livestack/shared";
import { InletNode, SpecNode, DefGraph } from "@livestack/shared";
import {
  StreamNode,
  getNodesConnectedToStream,
  getSourceSpecNodeConnectedToStream,
  InstantiatedGraph,
} from "@livestack/shared/src/graph/InstantiatedGraph";
import { v4 } from "uuid";
import { getLogger } from "../utils/createWorkerLogger";
import { longStringTruncator } from "../utils/longStringTruncator";
import { WrapWithTimestamp } from "./../utils/io";
import {
  InferDefaultOrSingleKey,
  LiveWorkerDef,
  LiveWorkerDefParams,
} from "./LiveWorker";
import pLimit from "p-limit";
import { Observable, Subscription } from "rxjs";
import { z } from "zod";
import { TagMaps, TagObj } from "../liveflow/Liveflow";
import {
  ByTagInput,
  ByTagOutput,
  WrapTerminatorAndDataId,
  wrapStreamSubscriberWithTermination,
  wrapTerminatorAndDataId,
} from "../utils/io";
import { DataStream } from "../stream/DataStream";
import { DataStreamSubscriber } from "../stream/DataStreamSubscriber";
import { LiveEnv } from "../env/LiveEnv";
import { resolveInstantiatedGraph } from "../liveflow/resolveInstantiatedGraph";
import { lruCacheFn } from "@livestack/shared";

const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

// export type CheckTMap<T> = T extends Record<string, infer V> ? T : never;

export type CheckSpec<SP> = SP extends JobSpec<
  infer P,
  infer I,
  infer O,
  infer IMap,
  infer OMap
>
  ? JobSpec<P, I, O, IMap, OMap>
  : never;

export type InferOutputType<
  Spec,
  K,
  OMap = InferStreamSetType<CheckSpec<Spec>["output"]>
> = OMap[K extends keyof OMap ? K : never] | null;

export type InferInputType<
  Spec,
  K,
  IMap = InferStreamSetType<CheckSpec<Spec>["input"]>
> = IMap[K extends keyof IMap ? K : never] | null;

/**
 * Defines a job specification including its input, output, and processing logic.
 * @template P - The type of the job parameters.
 * @template I - The type of the input stream set.
 * @template O - The type of the output stream set.
 * @template IMap - The mapped type of the input stream set.
 * @template OMap - The mapped type of the output stream set.
 */
export class JobSpec<
  P = {},
  I = never,
  O = never,
  IMap = InferTMap<I>,
  OMap = InferTMap<O>
> extends IOSpec<I, O, IMap, OMap> {
  private readonly _liveEnv: LiveEnv | null = null;
  protected static _registryBySpecName: Record<
    string,
    JobSpec<any, any, any, any, any>
  > = {};

  protected logger: ReturnType<typeof getLogger>;
  private _defGraph: DefGraph | null = null;

  public getDefGraph() {
    if (!this._defGraph) {
      this._defGraph = initDefGraph({
        root: {
          name: this.name,
          inputTags: Object.keys(this.input).map((t) => t.toString()),
          outputTags: Object.keys(this.output).map((t) => t.toString()),
        },
      });
    }
    return this._defGraph;
  }
  readonly jobOptions: z.ZodType<P>;

  /**
   * Creates a new JobSpec instance.
   * @param params - The parameters for constructing the JobSpec.
   * @param params.name - The name of the job spec.
   * @param params.jobOptions - Optional Zod schema defining the job options.
   * @param params.liveEnv - Optional LiveEnv instance.
   * @param params.input - Optional input stream set definition.
   * @param params.output - Optional output stream set definition.
   */
  constructor({
    liveEnv,
    name,
    jobOptions,
    output,
    input,
  }: {
    name: string;
    jobOptions?: z.ZodType<P>;
    liveEnv?: LiveEnv;
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
    this._liveEnv = liveEnv || null;
    this.logger = getLogger(`spec:${this.name}`);
    // if (!output) {
    //   this.logger.warn(`No output defined for job spec ${this.name}.`);
    // }
    JobSpec._registryBySpecName[this.name] = this;

    if (liveEnv) {
      this.liveEnvP = Promise.resolve(liveEnv);
    } else {
      this.liveEnvP = Promise.race([LiveEnv.globalP()]);
    }
    this.liveEnvPWithTimeout = Promise.race([
      this.liveEnvP,
      // new Promise<LiveEnv>((_, reject) => {
      //   setTimeout(() => {
      //     reject(
      //       new Error(
      //         "Livestack waited for LiveEnv to be set for over 10s, but is still not set. Please provide liveEnv either in the constructor of jobSpec, or globally with LiveEnv.setGlobalP."
      //       )
      //     );
      //   }, 1000 * 10);
      // }),
    ]);
  }

  public liveEnvP: Promise<LiveEnv>;
  public liveEnvPWithTimeout: Promise<LiveEnv>;

  public static lookupByName(specName: string) {
    if (!JobSpec._registryBySpecName[specName]) {
      throw new Error(`JobSpec ${specName} not defined on this machine.`);
    }
    return JobSpec._registryBySpecName[specName];
  }

  public derive<NewP, NewI, NewO, NewIMap, NewOMap>(
    newP: Partial<
      ConstructorParameters<typeof JobSpec<NewP, NewIMap, NewOMap>>[0]
    > & {
      name: string;
    }
  ) {
    if (newP.name === this.name) {
      throw new Error(
        `Derived job spec must have a different name from the original job spec ${this.name}.`
      );
    }
    return new JobSpec<
      P & NewP,
      NewI & I,
      NewO & O,
      IMap & NewIMap,
      OMap & NewOMap
    >({
      ...(this as any),
      input: this.__inputDef,
      output: this.__outputDef,
      ...newP,
      name: newP.name,
    } as ConstructorParameters<typeof JobSpec<P & NewP, NewI & I, NewO & O, IMap & NewIMap, OMap & NewOMap>>[0]);
  }

  /**
   * Enqueues a job and waits for a single result from a specific output tag.
   * @template K - The key type of the output stream set.
   * @param params - The parameters for enqueuing the job.
   * @param params.jobId - Optional job ID. If not provided, a unique ID will be generated.
   * @param params.jobOptions - Optional job options.
   * @param params.outputTag - Optional output tag to wait for the result. If not provided, the default or single output tag will be used.
   * @returns A promise that resolves to the job result containing the data and timestamp, or null if no result is available.
   */
  public async enqueueJobAndWaitOnSingleResults<K extends keyof OMap>({
    jobId: jobId,
    jobOptions,
    outputTag,
  }: {
    jobId?: string;
    jobOptions?: P;
    outputTag?: K;
  }): Promise<{
    data: OMap[K | InferDefaultOrSingleKey<OMap>];
    timestamp: number;
  } | null> {
    if (!jobId) {
      jobId = `${this.name}-${v4()}`;
    }

    const resolvedTag: K | InferDefaultOrSingleKey<OMap> =
      outputTag ||
      (this.getSingleTag("output", true) as K | InferDefaultOrSingleKey<OMap>);

    this.logger.info(
      `Enqueueing job ${jobId} with data: ${JSON.stringify(jobOptions)}.`
    );

    const { output, input } = await this.enqueueJob({
      jobId,
      jobOptions,
    });

    const r = await output(resolvedTag).nextValue();
    input.tags.forEach((k) => {
      input.terminate(k);
    });
    return r;
  }

  async getJobManager(jobId: string) {
    const liveEnv = await this.liveEnvPWithTimeout;
    const jobRecResp = await liveEnv.vaultClient.db.getJobRec({
      projectUuid: liveEnv.projectUuid,
      jobId,
      specName: this.name,
    });

    if (!!jobRecResp.null_response) {
      throw new Error(`Job ${jobId} not found.`);
    }

    const input = this._deriveInputsForJob(jobId);
    const output = this._deriveOutputsForJob(jobId);

    const instaG = await resolveInstantiatedGraph({
      specName: this.name,
      jobId,
      liveEnv: await this.liveEnvPWithTimeout,
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
    tag,
  }: {
    jobId: string;
    data: IMap[K extends never ? InferDefaultOrSingleKey<IMap> : K];
    tag?: K;
  }) {
    const resolvedTag =
      tag ||
      (this.getSingleTag("input", true) as K | InferDefaultOrSingleKey<IMap>);
    return this._deriveInputsForJob(jobId)(resolvedTag).feed(data as any);
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
    parentDatapoints,
  }: {
    jobId: string;
    tag: T extends "in" ? keyof IMap : keyof OMap;
    type: T;
    data: WrapTerminatorAndDataId<
      T extends "in" ? IMap[keyof IMap] : OMap[keyof OMap]
    >;
    parentDatapoints: {
      streamId: string;
      datapointId: string;
    }[];
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
      const stream = await this.getJobStream({
        jobId,
        tag: tag,
        type,
      });

      if (!d.terminate) {
        const [lastV] = await stream.valuesByReverseIndex(0);
        if (lastV?.terminate) {
          this.logger.error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream! jobId: ${jobId}, tag: ${String(tag)}`
          );
          console.error("data to send: ", d);
          throw new Error(
            `Cannot send ${
              type === "in" ? "input" : "output"
            } to a terminated stream!`
          );
        }
      }

      // await ensureStreamNotTerminated({
      //   liveEnv: this.liveEnvEnsured,
      //   logger: this.logger,
      //   jobId,
      //   type,
      //   tag,
      //   data: d,
      // });

      // console.debug(
      //   "stream_data",
      //   stream.uniqueName,
      //   JSON.stringify(d, longStringTruncator)
      // );

      const r = await stream.pub({
        message: d,
        parentDatapoints,
        ...(type === "out"
          ? {
              jobInfo: {
                jobId,
                outputTag: String(tag),
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
      specName: this.name,
      jobId,
      liveEnv: await this.liveEnvPWithTimeout,
    });

    const streamNodeId = instaG.findStreamNodeIdConnectedToJob({
      jobId,
      type,
      tag: p.tag.toString(),
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
    })) as DataStream<WrapTerminatorAndDataId<unknown>>;
  };

  public getOutputJobStream = async <K extends keyof OMap>(p: {
    jobId: string;
    tag: K;
  }) => {
    return (await this.getJobStream({
      ...p,
      type: "out",
    })) as DataStream<WrapTerminatorAndDataId<OMap[K]>>;
  };

  protected getJobStream = lruCacheFn(
    ({ jobId, type, tag }) => `${this.name}--${jobId}::${type}/${tag}`,
    async <
      TT extends "in" | "out",
      K extends TT extends "in" ? keyof IMap : keyof OMap
    >(p: {
      jobId: string;
      type: TT;
      tag: K;
    }) => {
      const { jobId, type } = p;

      type T = TT extends "in" ? IMap[keyof IMap] : OMap[keyof OMap];

      const specTagInfo = this.convertLiveflowAliasToSpecTag({
        alias: p.tag,
        type,
      });

      // connected spec may not be the spec responsible for the stream's construct
      const connectedSpec = JobSpec.lookupByName(specTagInfo.specName);

      const connectedJobId = await this.lookUpChildJobIdByGroupIDAndSpecTag({
        groupId: jobId,
        specInfo: specTagInfo,
      });

      const streamId = await connectedSpec.getStreamIdForJob({
        jobId: connectedJobId,
        type,
        p: specTagInfo,
      });

      // obtain the def of the stream
      let def: z.ZodType<WrapTerminatorAndDataId<T>> | null;
      if (type === "out") {
        def = wrapTerminatorAndDataId(
          connectedSpec.output[
            (specTagInfo.tag ||
              connectedSpec.getSingleTag("output", true)) as keyof OMap
          ].def
        ) as z.ZodType<WrapTerminatorAndDataId<T>>;
      } else if (type === "in") {
        // in principle, always try to find the source of the stream and use its def first
        const instaG = await resolveInstantiatedGraph({
          specName: this.name,
          jobId,
          liveEnv: await this.liveEnvPWithTimeout,
        });
        // console.log(
        //   "streamSourceSpecTypeByStreamId",
        //   this.name,
        //   instaG.streamSourceSpecTypeByStreamId
        // );
        if (instaG.streamSourceSpecTypeByStreamId[streamId]) {
          const sourceSpec = JobSpec.lookupByName(
            instaG.streamSourceSpecTypeByStreamId[streamId].specName
          );
          const source =
            sourceSpec.output[
              instaG.streamSourceSpecTypeByStreamId[streamId].tag
            ].def;
          def = wrapTerminatorAndDataId(source) as z.ZodType<
            WrapTerminatorAndDataId<T>
          >;
        } else {
          // check if inlet node has a transform
          const connectedJobNodeId = instaG.findNode((nId) => {
            const n = instaG.getNodeAttributes(nId);
            if (n.nodeType !== "job" && n.nodeType !== "root-job") {
              return false;
            } else {
              return n.jobId === connectedJobId;
            }
          });

          if (!connectedJobNodeId) {
            throw new Error(
              `Connected job node not found for job ${jobId} and tag ${p.tag.toString()}`
            );
          }

          const inletNodeId = instaG.findNode((nId) => {
            const n = instaG.getNodeAttributes(nId);
            if (n.nodeType !== "Inlet") {
              return false;
            } else {
              const ee = instaG.findOutboundEdge((eId) => {
                return instaG.target(eId) === connectedJobNodeId;
              });
              return !!ee;
            }
          });

          const inletNode = instaG.getNodeAttributes(inletNodeId) as InletNode;
          if (!inletNode.hasTransform) {
            // no transform; proceed as usual
            def = wrapTerminatorAndDataId(
              connectedSpec.input[
                (specTagInfo.tag ||
                  connectedSpec.getSingleTag("input", true)) as keyof IMap
              ].def
            ) as z.ZodType<WrapTerminatorAndDataId<T>>;
          } else {
            // try to find source of the stream
            const streamNodeId = instaG.findStreamNodeIdConnectedToJob({
              jobId: connectedJobId,
              type: "in",
              tag: specTagInfo.tag,
            });
            if (!streamNodeId) {
              throw new Error(
                `Stream node not found for job ${connectedJobId} and tag ${specTagInfo.tag}`
              );
            }

            const source = getSourceSpecNodeConnectedToStream(
              instaG,
              streamNodeId
            );
            if (!source) {
              this.logger.warn(
                `A transform function is applied to the input stream ${streamId} of job ${connectedJobId} but its definition is unclear. Runtime type checking will be skipped for this stream.`
              );
              def = null;
            } else {
              const responsibleSpec = JobSpec.lookupByName(
                source.origin.specName
              );
              def = wrapTerminatorAndDataId(
                responsibleSpec.output[
                  (source.outletNode.tag ||
                    responsibleSpec.getSingleTag("output", true)) as keyof OMap
                ].def
              ) as z.ZodType<WrapTerminatorAndDataId<T>>;
            }
          }
        }
        // TODO: def should not just come from input spec.
        // If the stream connected to an output spec, it should always
        // come from the output if there's a transform.
      } else {
        throw new Error(`Invalid type ${type}`);
      }

      const stream = await DataStream.getOrCreate<WrapTerminatorAndDataId<T>>({
        uniqueName: streamId,
        def,
        logger: connectedSpec.logger,
        liveEnv: await connectedSpec.liveEnvPWithTimeout,
      });

      return stream as DataStream<WrapTerminatorAndDataId<T>>;
    }
  );

  private _outputCollectorByJobIdAndTag: {
    [k: `${string}/${string}[${string}]/${string}`]: Promise<
      ByTagOutput<OMap[keyof OMap]>
    >;
  } = {};

  public createOutputCollector<K extends keyof OMap>({
    jobId,
    tag,
    from = "beginning",
  }: {
    jobId: string;
    tag?: K;
    from?: "beginning" | "now";
  }): ByTagOutput<OMap[K extends never ? InferDefaultOrSingleKey<OMap> : K]> {
    const tagToWatch = tag || this.getSingleTag("output", true);
    const streamP = this.getOutputJobStream({
      jobId,
      tag: tagToWatch,
    });
    const subuscriberP = new Promise<
      DataStreamSubscriber<
        WrapTerminatorAndDataId<
          OMap[K extends never ? InferDefaultOrSingleKey<OMap> : K]
        >
      >
    >((resolve, reject) => {
      streamP.then((stream) => {
        // console.debug("Output collector for stream", stream.uniqueName);
        let subscriber: DataStreamSubscriber<
          // TODO: fix tying
          WrapTerminatorAndDataId<OMap[any]>
        >;
        if (from === "beginning") {
          subscriber = DataStreamSubscriber.subFromBeginning(stream);
        } else if (from === "now") {
          subscriber = DataStreamSubscriber.subFromNow(stream);
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

  /**
   * Enqueues a new job with the given parameters.
   * @param params - The parameters for enqueuing the job.
   * @param params.jobId - Optional job ID. If not provided, a unique ID will be generated.
   * @param params.jobOptions - Optional job options.
   * @param params.parentJobId - Optional parent job ID.
   * @param params.uniqueSpecLabel - Optional unique label for the job spec.
   * @param params.inputStreamIdOverridesByTag - Optional overrides for input stream IDs by tag.
   * @param params.outputStreamIdOverridesByTag - Optional overrides for output stream IDs by tag.
   * @returns A promise that resolves to the JobManager instance for the enqueued job.
   */
  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: P;
    parentJobId?: string;
    uniqueSpecLabel?: string;
    inputStreamIdOverridesByTag?: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByTag?: Partial<Record<keyof OMap, string>>;
  }): Promise<JobManager<P, I, O, IMap, OMap>> {
    let {
      jobId = p?.jobId || "j-" + this.name + "-" + v4(),
      jobOptions,
      inputStreamIdOverridesByTag,
      outputStreamIdOverridesByTag,
    } = p || {};

    // console.debug("Spec._enqueueJob", jobId, jobOptions);

    const projectUuid = (await this.liveEnvPWithTimeout).projectUuid;

    // construct streamIdOverridesByTagByTypeByJobId[jobId] from the graph

    // construct a local instantiated graph to get default streamIdOverridesByTagByTypeByJobId
    const localG = new InstantiatedGraph({
      defGraph: this.getDefGraph(),
      contextId: jobId,
      rootJobId: jobId,
      streamIdOverrides: {},
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    if (!this.streamIdOverridesByTagByTypeByJobId[jobId]) {
      const inletEdgeIds = localG.inboundEdges(localG.rootJobId).filter((e) => {
        const node = localG.getNodeAttributes(localG.source(e));
        return node.nodeType === "Inlet";
      });
      const inletNodeIds = inletEdgeIds.map((e) => localG.source(e));
      const _in = Object.fromEntries(
        inletNodeIds.map((nId) => {
          const node = localG.getNodeAttributes(nId) as InletNode;
          const streamToInletEdgeId = localG.inboundEdges(nId)[0];
          const streamNodeId = localG.source(streamToInletEdgeId);
          const streamNode = localG.getNodeAttributes(
            streamNodeId
          ) as StreamNode;

          return [node.tag, streamNode.streamId];
        })
      ) as Record<keyof IMap, string>;

      const outletEdgeIds = localG
        .outboundEdges(localG.rootJobId)
        .filter((e) => {
          const node = localG.getNodeAttributes(localG.target(e));
          return node.nodeType === "Outlet";
        });
      const outletNodeIds = outletEdgeIds.map((e) => localG.target(e));
      const _out = Object.fromEntries(
        outletNodeIds.map((nId) => {
          const node = localG.getNodeAttributes(nId) as OutletNode;
          const streamToOutletEdgeId = localG.outboundEdges(nId)[0];
          const streamNodeId = localG.target(streamToOutletEdgeId);
          const streamNode = localG.getNodeAttributes(
            streamNodeId
          ) as StreamNode;

          return [node.tag, streamNode.streamId];
        })
      ) as Record<keyof OMap, string>;

      this.streamIdOverridesByTagByTypeByJobId[jobId] = {
        in: _in,
        out: _out,
      };
    }
    if (inputStreamIdOverridesByTag) {
      Object.assign(
        this.streamIdOverridesByTagByTypeByJobId[jobId]["in"]!,
        inputStreamIdOverridesByTag
      );
    }
    if (outputStreamIdOverridesByTag) {
      Object.assign(
        this.streamIdOverridesByTagByTypeByJobId[jobId]["out"]!,
        outputStreamIdOverridesByTag
      );
    }

    jobOptions = jobOptions || ({} as P);
    const vaultClient = await (await this.liveEnvP).vaultClient;
    await vaultClient.db.ensureJobAndStatusAndConnectorRecs({
      projectUuid: (await this.liveEnvPWithTimeout).projectUuid,
      specName: this.name,
      jobId,
      jobOptionsStr: JSON.stringify(jobOptions),
      parentJobId: p?.parentJobId,
      uniqueSpecLabel: p?.uniqueSpecLabel,
      inputStreamIdOverridesByTag:
        this.streamIdOverridesByTagByTypeByJobId[jobId]["in"]!,
      outputStreamIdOverridesByTag:
        this.streamIdOverridesByTagByTypeByJobId[jobId]["out"]!,
    });

    const j = await vaultClient.queue.addJob({
      projectUuid,
      specName: this.name,
      jobId,
      jobOptionsStr: JSON.stringify(jobOptions as any),
      contextId: p?.parentJobId,
    });

    await vaultClient.db.updateJobInstantiatedGraph({
      projectUuid: (await this.liveEnvPWithTimeout).projectUuid,
      jobId,
      specName: this.name,
      instantiatedGraphStr: JSON.stringify(localG),
    });

    this.logger.info(
      `Added job with ID ${jobId} to jobSpec ${this.name} with params ` +
        `${JSON.stringify(jobOptions, longStringTruncator)}`
    );

    const m = await this.getJobManager(jobId);
    return m;
  }

  /**
   * Enqueues a job with the given input, waits for the result, and returns it.
   * @template KI - The key type of the input stream set.
   * @template KO - The key type of the output stream set.
   * @param params - The parameters for enqueuing the job and getting the result.
   * @param params.jobId - Optional job ID. If not provided, a unique ID will be generated.
   * @param params.jobOptions - Optional job options.
   * @param params.input - The input data to feed into the job.
   * @param params.inputTag - Optional input tag to feed the data. If not provided, the default or single input tag will be used.
   * @param params.outputTag - Optional output tag to wait for the result. If not provided, the default or single output tag will be used.
   * @returns A promise that resolves to the job result containing the data and timestamp, or null if no result is available.
   */
  public enqueueAndGetResult = async <
    KI extends keyof IMap,
    KO extends keyof OMap
  >({
    jobId,
    jobOptions,
    input,
    inputTag,
    outputTag,
  }: {
    jobId?: string;
    jobOptions?: P;
    input: IMap[KI extends never ? InferDefaultOrSingleKey<IMap> : KI];
    inputTag?: KI;
    outputTag?: KO;
  }) => {
    const manager = await this.enqueueJob({
      jobOptions,
      jobId,
    });
    await manager.input(inputTag).feed(input);
    const out = await manager.output(outputTag).nextValue();
    manager.input.tags.forEach((k) => {
      manager.input.terminate(k);
    });
    return out;
  };

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
        return <K extends keyof IMap>(tag: K) => {
          let resolvedTag: K | InferDefaultOrSingleKey<IMap> | undefined = tag;
          return {
            feed: async (data: IMap[K]) => {
              if (!resolvedTag) {
                resolvedTag = that.getSingleTag("input", true) as
                  | K
                  | InferDefaultOrSingleKey<IMap>;
              }
              const specTagInfo = that.convertLiveflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = JobSpec.lookupByName(
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
                // input datapoints are oirin points; no parent datapoints
                parentDatapoints: [],
              });
            },
            terminate: async () => {
              if (!resolvedTag) {
                resolvedTag = that.getSingleTag("input", true);
              }
              const specTagInfo = that.convertLiveflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = JobSpec.lookupByName(
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
                // termination points have no parents
                parentDatapoints: [],
              });
            },
            getStreamId: async () => {
              if (!resolvedTag) {
                resolvedTag = that.getSingleTag("input", true) as
                  | K
                  | InferDefaultOrSingleKey<IMap>;
              }
              const specTagInfo = that.convertLiveflowAliasToSpecTag({
                alias: resolvedTag,
                type: "in",
              });
              const responsibleSpec = JobSpec.lookupByName(
                specTagInfo.specName
              );

              const responsibleJobId =
                await this.lookUpChildJobIdByGroupIDAndSpecTag({
                  groupId: jobId,
                  specInfo: specTagInfo,
                });

              const s = await responsibleSpec.getInputJobStream({
                jobId: responsibleJobId,
                tag: resolvedTag,
              });
              return s.uniqueName;
            },
          };
        };
      };
      const byTagFn = genByTagFn() as JobInput<IMap>;
      byTagFn.byTag = genByTagFn();
      byTagFn.tags = this.inputTags;

      byTagFn.feed = async (data: IMap[InferDefaultOrSingleKey<IMap>]) => {
        const tag = that.getSingleTag("input", true);
        if (!tag) {
          throw new Error(
            `Cannot find any input to feed to for spec ${this.name}.`
          );
        }
        return await byTagFn(tag).feed(data as IMap[any]);
      };

      byTagFn.terminate = async <K extends keyof IMap>(tag?: K) => {
        const resolvedTag = tag || that.getSingleTag("input", true);
        return await byTagFn(resolvedTag).terminate();
      };
      return byTagFn;
    })();
  };

  protected _getInputTags() {
    return Object.keys(this.input) as (keyof IMap)[];
  }
  protected _getOutputTags() {
    return Object.keys(this.output) as (keyof OMap)[];
  }

  public get inputTags() {
    return this._getInputTags();
  }

  public get outputTags() {
    return this._getOutputTags();
  }

  public get isInputSingle() {
    return this.inputTags.length === 1;
  }

  public get isOutputSingle() {
    return this.outputTags.length === 1;
  }

  public _deriveOutputsForJob: (jobId: string) => JobOutput<OMap> = (
    jobId: string
  ) => {
    const singletonSubscriberByTag = <K extends keyof OMap>(tag: K) => {
      const specTagInfo = this.convertLiveflowAliasToSpecTag({
        alias: tag,
        type: "out",
      });
      const responsibleSpec = JobSpec.lookupByName(specTagInfo.specName);

      // TODO: add cleanup for output collectors
      if (
        !this._outputCollectorByJobIdAndTag[
          `${jobId}/${specTagInfo.specName}[${
            specTagInfo.uniqueSpecLabel || "default"
          }]/${specTagInfo.tag.toString()}`
        ]
      ) {
        const responsibleJobIdP = this.lookUpChildJobIdByGroupIDAndSpecTag({
          groupId: jobId,
          specInfo: specTagInfo,
        });
        const subscriberP = responsibleJobIdP.then((responsibleJobId) =>
          responsibleSpec.createOutputCollector({
            jobId: responsibleJobId,
            tag: specTagInfo.tag,
          })
        );
        this._outputCollectorByJobIdAndTag[
          `${jobId}/${specTagInfo.specName}[${
            specTagInfo.uniqueSpecLabel || "default"
          }]/${specTagInfo.tag.toString()}`
        ] = subscriberP;
      }

      const subscriberP = this._outputCollectorByJobIdAndTag[
        `${jobId}/${specTagInfo.specName}[${
          specTagInfo.uniqueSpecLabel || "default"
        }]/${specTagInfo.tag.toString()}`
      ] as Promise<ByTagOutput<OMap[K]>>;

      const valueObservable = new Observable<WrapWithTimestamp<OMap[K]> | null>(
        (subs) => {
          let innerSubscription: Subscription | null = null;
          const initAsync = async () => {
            const subscriber = await subscriberP;
            innerSubscription = subscriber.valueObservable.subscribe({
              next: (v) => {
                if (v) {
                  subs.next(v);
                } else {
                  subs.complete();
                }
              },
              error: (err) => subs.error(err),
              complete: () => subs.complete(),
            });
          };
          initAsync();

          return () => {
            // This function is called when the observable is unsubscribed
            if (innerSubscription) {
              innerSubscription.unsubscribe();
            }
          };
        }
      );

      return {
        getStreamId: async () => await (await subscriberP).getStreamId(),
        nextValue: async () => await (await subscriberP).nextValue(),
        mostRecent: async (n?: number) =>
          await (await subscriberP).mostRecent(n),
        allValues: async () => await (await subscriberP).allValues(),
        valueObservable,
        async *[Symbol.asyncIterator]() {
          while (true) {
            const input = await (await subscriberP).nextValue();

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
      ReturnType<typeof singletonSubscriberByTag<InferDefaultOrSingleKey<OMap>>>
    > | null = null;

    const nextValue = async () => {
      if (subscriberByDefaultTag === null) {
        const tag = this.getSingleTag("output", true);
        subscriberByDefaultTag = await singletonSubscriberByTag(tag);
      }

      return await subscriberByDefaultTag.nextValue();
    };

    const valueObservable = new Observable<
      WrapWithTimestamp<OMap[InferDefaultOrSingleKey<OMap>]>
    >((subscriber) => {
      const initAsync = async () => {
        if (subscriberByDefaultTag === null) {
          const tag = this.getSingleTag("output", true);
          subscriberByDefaultTag = await singletonSubscriberByTag(tag);
        }

        const innerSubscription =
          subscriberByDefaultTag.valueObservable.subscribe({
            next: (v) => {
              if (v) {
                subscriber.next(v);
              } else {
                subscriber.complete();
              }
            },
            error: (err) => subscriber.error(err),
            complete: () => subscriber.complete(),
          });

        return innerSubscription; // return this for cleanup
      };

      const subscriptionPromise = initAsync();

      return () => {
        // This function is called when the observable is unsubscribed
        subscriptionPromise.then((subscription) => subscription.unsubscribe());
      };
    });

    const mostRecent = async (n?: number) => {
      if (subscriberByDefaultTag === null) {
        const tag = this.getSingleTag("output", true);
        subscriberByDefaultTag = await singletonSubscriberByTag(tag);
      }

      return await subscriberByDefaultTag.mostRecent(n);
    };

    return (() => {
      const func = (<K extends keyof OMap>(tag?: K) => {
        const resolvedTag = tag || this.getSingleTag("output", true);
        return singletonSubscriberByTag(resolvedTag) as any;
      }) as JobOutput<OMap>;
      func.byTag = singletonSubscriberByTag;
      func.tags = this.outputTags;
      func.nextValue = nextValue;
      func.mostRecent = mostRecent;
      func.valueObservable = valueObservable;
      func[Symbol.asyncIterator] = async function* () {
        while (true) {
          const input = await nextValue();

          // Assuming nextInput returns null or a similar value to indicate completion
          if (!input) {
            break;
          }
          yield input;
        }
      };
      return func;
    })();
  };

  protected convertSpecTagToLiveflowAlias({
    tag,
  }: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
  }) {
    return tag;
  }

  protected convertLiveflowAliasToSpecTag({
    type,
    alias,
  }: {
    type: "in" | "out";
    alias: string | symbol | number;
  }): {
    specName: string;
    tag: string;
    direction: "in" | "out";
    uniqueSpecLabel?: string;
  } {
    return {
      specName: this.name,
      tag: alias.toString(),
      direction: type,
      uniqueSpecLabel: undefined as string | undefined,
    };
  }

  public getSingleTag<
    T extends "input" | "output",
    ThrowErr extends true | false
  >(
    type: T,
    throwError: ThrowErr
  ):
    | (T extends "input"
        ? InferDefaultOrSingleKey<IMap>
        : InferDefaultOrSingleKey<OMap>)
    | (ThrowErr extends true
        ? never
        : {
            error: "cannot-find-tag" | "multiple-tags-found";
          }) {
    // naive implementation: find spec node with only one input
    // or output which is not connected to another spec, and return its tag
    const defG = this.getDefGraph();
    // convert Uint32Array to number[]
    let specNodeIds = Array.from(defG.getSpecNodeIds());
    if (specNodeIds.length === 0) {
      const rootSpecNodeId = defG.getRootSpecNodeId();
      specNodeIds = [rootSpecNodeId];
    } else {
      specNodeIds = Array.from(defG.getSpecNodeIds());
    }

    const pass0 = specNodeIds.map((specNodeId) => {
      const specNode = defG.getNodeAttributes(specNodeId) as SpecNode;
      if (type === "input") {
        return {
          connectedStreamNodes: defG
            .getInboundStreamNodes(specNodeId)
            .results.map((s) => ({
              specName: specNode.specName,
              uniqueSpecLabel: specNode.uniqueSpecLabel,
              type: "in" as const,
              tag: s.inletNode.tag!,
              inletNodeId: s.inletNode.id,
              streamNodeId: s.streamNode.id,
            })),
          specNodeId,
        };
      } else {
        return {
          connectedStreamNodes: defG
            .getOutboundStreamNodes(specNodeId)
            .results.map((s) => ({
              specName: specNode.specName,
              uniqueSpecLabel: specNode.uniqueSpecLabel,
              type: "out" as const,
              tag: s.outletNode.tag!,
              outletNodeId: s.outletNode.id,
              streamNodeId: s.streamNode.id,
            })),
          specNodeId,
        };
      }
    });

    const pass1 = pass0.filter(
      ({ connectedStreamNodes }) =>
        connectedStreamNodes.length === 1 ||
        connectedStreamNodes.some((c) => c.tag === "default")
    );
    const pass2 = pass1
      .filter(({ connectedStreamNodes }) => {
        const conn = connectedStreamNodes[0];
        if (type === "input") {
          const { source } = getNodesConnectedToStream(defG, conn.streamNodeId);
          return !source;
        } else {
          const { targets } = getNodesConnectedToStream(
            defG,
            conn.streamNodeId
          );
          return targets.length === 0;
        }
      })
      .map((qualified) => ({
        ...qualified,
        connectedStreamNodes: qualified.connectedStreamNodes.map((c) => ({
          ...c,
          alias: this.convertSpecTagToLiveflowAlias(c),
        })),
      }));
    const qualified = pass2;
    if (qualified.length === 0) {
      // if (throwError) {
      throw new Error(
        `Cannot identify a single unambiguous ${type} for spec "${
          this.name
        }". Please specify at least one in the "${type}" field of the spec's definition. Available ${type}s: [${(type ===
        "input"
          ? this.inputTags
          : this.outputTags
        )
          .map((s) => `"${s.toString()}"`)
          .join(", ")}]}`
      );
      // } else {
      //   return { error: "cannot-find-tag" } as ThrowErr extends true
      //     ? never
      //     : { error: "cannot-find-tag" };
      // }
    } else if (qualified.length > 1) {
      // if (throwError) {
      throw new Error(
        `Ambiguous ${type} for spec "${this.name}"; found more than two child specs with a single ${type}. \nPlease specify which one to use with "${type}(tagName)".`
      );
      // } else {
      //   return { error: "multiple-tags-found" } as ThrowErr extends true
      //     ? never
      //     : { error: "multiple-tags-found" };
      // }
    } else {
      const qualifiedC =
        qualified[0].connectedStreamNodes.length === 1
          ? qualified[0].connectedStreamNodes[0]
          : qualified[0].connectedStreamNodes.find((c) => c.tag === "default")!;
      if (!qualifiedC.alias) {
        // the user didn't give a public tag; auto-register
      }
      const t =
        (qualifiedC.alias as T extends "input"
          ? InferDefaultOrSingleKey<IMap>
          : InferDefaultOrSingleKey<OMap>) || null;

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
  //   spec: Spec<any, any, any>;
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
  //   const childSpec = Spec.lookupByName(specInfo.spec.name);

  //   return { spec: childSpec, jobId };
  // }

  public toString() {
    return this.name;
  }

  toJSON() {
    return this.name;
  }

  // convenience function
  /**
   * Defines a worker for the job spec.
   * @template WP - The type of the worker parameters.
   * @param params - The parameters for defining the worker, excluding the job spec.
   * @returns The created LiveWorkerDef instance.
   */
  public defineWorker<WP extends object | undefined>(
    p: Omit<LiveWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec">
  ) {
    return new LiveWorkerDef<P, I, O, WP, IMap, OMap>({
      ...p,
      jobSpec: this,
    });
  }

  /**
   * Defines a worker for the job spec and starts it.
   * @template WP - The type of the worker parameters.
   * @param params - The parameters for defining and starting the worker.
   * @param params.instanceParams - Optional instance parameters for the worker.
   * @returns A promise that resolves to the created LiveWorkerDef instance.
   */
  public async defineWorkerAndStart<WP extends object | undefined>({
    instanceParams,
    ...p
  }: Omit<LiveWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }

  /**
   * Creates a new JobSpec instance with the given parameters.
   * @template P - The type of the job parameters.
   * @template I - The type of the input stream set.
   * @template O - The type of the output stream set.
   * @param params - The parameters for constructing the JobSpec.
   * @returns The created JobSpec instance.
   */
  public static define<P, I, O>(
    p: ConstructorParameters<
      typeof JobSpec<P, I, O, InferTMap<I>, InferTMap<O>>
    >[0]
  ) {
    return new JobSpec<P, I, O, InferTMap<I>, InferTMap<O>>(p);
  }

  static isJobSpec = (x: any): x is JobSpec<any, any, any, any, any> => {
    return x instanceof JobSpec;
  };
}

export class JobManager<P, I, O, IMap, OMap> {
  public readonly input: JobInput<IMap>;
  public readonly output: JobOutput<OMap>;

  public readonly jobId: string;
  public readonly graph: InstantiatedGraph;
  public readonly spec: JobSpec<P, I, O, IMap, OMap>;

  private _finishedPromise: Promise<void> | null = null;

  constructor({
    spec,
    jobId,
    input,
    output,
    instantiatedGraph,
  }: {
    spec: JobSpec<P, I, O, IMap, OMap>;
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
    this.spec = spec;
    // this._finishedPromise = this._genWaitUntilFinishPromise();
  }

  private _genWaitUntilFinishPromise = async () => {
    const outputsToWaitOn: (keyof OMap)[] = [];

    const maybeSingleOutputTag = this.spec.getSingleTag("output", false);
    if (typeof maybeSingleOutputTag === "string") {
      outputsToWaitOn.push(maybeSingleOutputTag);
    } else {
      // wait on all outputs
      outputsToWaitOn.push(
        ...this.output.tags.map((t) => t.toString() as keyof OMap)
      );
    }
    // console.log(
    //   "JobManager: ",
    //   this.jobId,
    //   "waiting on outputs",
    //   outputsToWaitOn
    // );
    await Promise.all(
      outputsToWaitOn.map(async (outputTag) => {
        for await (const _ of this.output(outputTag)) {
          // do nothing until loop ends
        }
      })
    );

    // console.log("JobManager: Job finished", this.jobId);
  };

  public waitUntilFinish = () => {
    if (!this._finishedPromise) {
      this._finishedPromise = this._genWaitUntilFinishPromise();
    }
    return this._finishedPromise;
  };
}
export interface JobInput<IMap> {
  <K extends keyof IMap>(tag?: K): ByTagInput<
    IMap[K extends never ? InferDefaultOrSingleKey<IMap> : K]
  >;
  tags: (keyof IMap)[];
  feed: (data: IMap[InferDefaultOrSingleKey<IMap>]) => Promise<void>;
  terminate: <K extends keyof IMap>(tag?: K) => Promise<void>;
  byTag: <K extends keyof IMap>(tag: K) => ByTagInput<IMap[K]>;
}

export interface JobOutput<OMap> {
  <K extends keyof OMap>(tag?: K): ByTagOutput<OMap[K]>;
  tags: (keyof OMap)[];
  byTag: <K extends keyof OMap>(tag: K) => ByTagOutput<OMap[K]>;
  nextValue: () => Promise<WrapWithTimestamp<
    OMap[InferDefaultOrSingleKey<OMap>]
  > | null>;
  mostRecent: (
    n?: number
  ) => Promise<WrapWithTimestamp<OMap[InferDefaultOrSingleKey<OMap>]>[]>;
  allValues: () => Promise<WrapWithTimestamp<OMap[keyof OMap]>[]>;
  valueObservable: Observable<WrapWithTimestamp<
    OMap[InferDefaultOrSingleKey<OMap>]
  > | null>;
  [Symbol.asyncIterator]: () => AsyncIterableIterator<{
    data: OMap[keyof OMap];
    timestamp: number;
  }>;
}
export const SpecOrName = z.union([
  z.string(),
  z.instanceof(JobSpec<any, any, any, any, any>),
]);
export type SpecOrName<P = any, I = any, O = any, IMap = any, OMap = any> =
  | string
  | JobSpec<P, I, O, IMap, OMap>;

export function convertSpecOrName(
  specOrName: SpecOrName | TagObj<any, any, any, any, any, any, any>
) {
  if (typeof specOrName === "string") {
    return JobSpec.lookupByName(specOrName);
  } else if (specOrName instanceof JobSpec) {
    return specOrName;
  } else if (specOrName.spec instanceof JobSpec) {
    return convertSpecOrName(specOrName.spec);
  } else {
    throw new Error("Invalid spec");
  }
}
 function resolveTagMapping(
   p:
     | {
         _aliasMaps?: Partial<TagMaps<any, any, any, any, any, any>>;
       }
     | string
     | any
 ) {
   let inputAliasMap: Record<string, string> = {};
   let outputAliasMap: Record<string, string> = {};

   if (typeof p === "object" && !(p instanceof JobSpec) && p._aliasMaps) {
     inputAliasMap = p._aliasMaps.inputTag || {};
     outputAliasMap = p._aliasMaps.outputTag || {};
   }
   return {
     inputAliasMap,
     outputAliasMap,
   };
 }
