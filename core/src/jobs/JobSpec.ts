import {
  InferStreamSetType,
  IOSpec,
  InferTMap,
  wrapIfSingle,
  initDefGraph,
} from "@livestack/shared";
import { InletNode, SpecNode, DefGraph } from "@livestack/shared";
import {
  StreamNode,
  getNodesConnectedToStream,
  getSourceSpecNodeConnectedToStream,
  InstantiatedGraph,
} from "@livestack/shared/src/graph/InstantiatedGraph";
import {
  AuthorizedGRPCClient,
  genAuthorizedVaultClient,
} from "@livestack/vault-client";
import { v4 } from "uuid";
import { getLogger } from "../utils/createWorkerLogger";
import { longStringTruncator } from "../utils/longStringTruncator";
import { WrapWithTimestamp } from "./../utils/io";
import { ZZWorkerDef, ZZWorkerDefParams } from "./ZZWorker";
import pLimit from "p-limit";
import { Observable } from "rxjs";
import { z } from "zod";
import { TagMaps, TagObj } from "../orchestrations/Workflow";
import { Metadata } from "nice-grpc-common";
import {
  ByTagInput,
  ByTagOutput,
  WrapTerminatorAndDataId,
  wrapStreamSubscriberWithTermination,
  wrapTerminatorAndDataId,
} from "../utils/io";
import { DataStream, WithTimestamp } from "../streams/DataStream";
import { DataStreamSubscriber } from "../streams/DataStreamSubscriber";
import { ZZEnv } from "./ZZEnv";
import { resolveInstantiatedGraph } from "../orchestrations/resolveInstantiatedGraph";
import { lruCacheFn } from "../utils/lruCacheFn";

export const JOB_ALIVE_TIMEOUT = 1000 * 60 * 10;

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

export class JobSpec<
  P = {},
  I = never,
  O = never,
  IMap = InferTMap<I>,
  OMap = InferTMap<O>
> extends IOSpec<I, O, IMap, OMap> {
  private readonly _zzEnv: ZZEnv | null = null;
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
    JobSpec._registryBySpecName[this.name] = this;

    if (zzEnv) {
      this.zzEnvP = Promise.resolve(zzEnv);
    } else {
      this.zzEnvP = Promise.race([ZZEnv.globalP()]);
    }
    this.zzEnvPWithTimeout = Promise.race([
      this.zzEnvP,
      new Promise<ZZEnv>((_, reject) => {
        setTimeout(() => {
          reject(
            new Error(
              "Livestack waited for ZZEnv to be set for over 10s, but is still not set. Please provide zzEnv either in the constructor of jobSpec, or globally with ZZEnv.setGlobalP."
            )
          );
        }, 1000 * 10);
      }),
    ]);
  }

  public zzEnvP: Promise<ZZEnv>;
  public zzEnvPWithTimeout: Promise<ZZEnv>;

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
      specName: this.name,
      jobId,
      zzEnv: await this.zzEnvPWithTimeout,
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
      const stream = await this.getJobStream({
        jobId,
        tag: tag,
        type,
      });

      if (!d.terminate) {
        const lastV = await stream.valueByReverseIndex(0);
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
      //   zzEnv: this.zzEnvEnsured,
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

      await stream.pub({
        message: d,
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
      zzEnv: await this.zzEnvPWithTimeout,
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

      const specTagInfo = this.convertWorkflowAliasToSpecTag({
        alias: p.tag,
        type,
      });

      // connected spec may not be the spec responsible for the stream's shape
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

      let def: z.ZodType<WrapTerminatorAndDataId<T>> | null;
      if (type === "out") {
        def = wrapTerminatorAndDataId(
          connectedSpec.output[
            (specTagInfo.tag ||
              connectedSpec.getSingleOutputTag()) as keyof OMap
          ].def
        ) as z.ZodType<WrapTerminatorAndDataId<T>>;
      } else if (type === "in") {
        // in principle, always try to find the source of the stream and use its def first
        const instaG = await resolveInstantiatedGraph({
          specName: this.name,
          jobId,
          zzEnv: await this.zzEnvPWithTimeout,
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
                  connectedSpec.getSingleInputTag()) as keyof IMap
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
                responsibleSpec.input[
                  (source.outletNode.tag ||
                    responsibleSpec.getSingleOutputTag()) as keyof OMap
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
        zzEnv: await connectedSpec.zzEnvPWithTimeout,
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
  }): ByTagOutput<OMap[K]> {
    const tagToWatch = tag || (this.getSingleOutputTag() as K);
    const streamP = this.getOutputJobStream({
      jobId,
      tag: tagToWatch,
    });
    const subuscriberP = new Promise<
      DataStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>
    >((resolve, reject) => {
      streamP.then((stream) => {
        // console.debug("Output collector for stream", stream.uniqueName);
        let subscriber: DataStreamSubscriber<WrapTerminatorAndDataId<OMap[K]>>;
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

  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: P;
    parentJobId?: string;
    uniqueSpecLabel?: string;
    inputStreamIdOverridesByTag?: Partial<Record<keyof IMap, string>>;
    outputStreamIdOverridesByTag?: Partial<Record<keyof OMap, string>>;
  }) {
    let {
      jobId = p?.jobId || "j-" + this.name + "-" + v4(),
      jobOptions,
      inputStreamIdOverridesByTag,
      outputStreamIdOverridesByTag,
    } = p || {};

    // console.debug("Spec._enqueueJob", jobId, jobOptions);

    const projectId = (await this.zzEnvPWithTimeout).projectId;
    if (!this.streamIdOverridesByTagByTypeByJobId[jobId]) {
      this.streamIdOverridesByTagByTypeByJobId[jobId] = {
        in: null,
        out: null,
      };
    }
    if (inputStreamIdOverridesByTag) {
      this.streamIdOverridesByTagByTypeByJobId[jobId]["in"] =
        inputStreamIdOverridesByTag;
    }
    if (outputStreamIdOverridesByTag) {
      this.streamIdOverridesByTagByTypeByJobId[jobId]["out"] =
        outputStreamIdOverridesByTag;
    }

    jobOptions = jobOptions || ({} as P);
    const instaG = await resolveInstantiatedGraph({
      specName: this.name,
      jobId,
      zzEnv: await this.zzEnvPWithTimeout,
    });
    const vaultClient = await ZZEnv.vaultClient();
    await vaultClient.db.ensureJobAndStatusAndConnectorRecs({
      projectId: (await this.zzEnvPWithTimeout).projectId,
      specName: this.name,
      jobId,
      jobOptionsStr: JSON.stringify(jobOptions),
      parentJobId: p?.parentJobId,
      uniqueSpecLabel: p?.uniqueSpecLabel,
      inputStreamIdOverridesByTag: inputStreamIdOverridesByTag || {},
      outputStreamIdOverridesByTag: outputStreamIdOverridesByTag || {},
      instantGraphStr: JSON.stringify(instaG.toJSON()),
    });

    const j = await vaultClient.queue.addJob({
      projectId,
      specName: this.name,
      jobId,
      jobOptionsStr: JSON.stringify(jobOptions as any),
      contextId: p?.parentJobId,
    });

    this.logger.info(
      `Added job with ID ${jobId} to jobSpec with params ` +
        `${JSON.stringify(jobOptions, longStringTruncator)}`
    );

    const m = await this.getJobManager(jobId);
    return m;
  }

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
    input: IMap[KI];
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
        return <K extends keyof IMap>(tag?: K) => {
          const resolvedTag = tag || (that.getSingleInputTag() as K);
          return {
            feed: async (data: IMap[K]) => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
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
              });
            },
            terminate: async () => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
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
              });
            },
            getStreamId: async () => {
              const specTagInfo = that.convertWorkflowAliasToSpecTag({
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
      const specTagInfo = this.convertWorkflowAliasToSpecTag({
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

      return {
        getStreamId: async () => await (await subscriberP).getStreamId(),
        nextValue: async () => await (await subscriberP).nextValue(),
        mostRecentValue: async () =>
          await (await subscriberP).mostRecentValue(),
        valueObservable: new Observable<WrapWithTimestamp<OMap[K]> | null>(
          (subs) => {
            subscriberP.then(async (subscriber) => {
              while (true) {
                const input = await subscriber.nextValue();
                if (!input) {
                  subs.complete();
                  subs.unsubscribe();
                  break;
                }
                subs.next(input);
              }
            });
            return () => {
              subs.complete();
              subs.unsubscribe();
            };
          }
        ),
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
      func.tags = this.outputTags;
      func.nextValue = nextValue;
      (func.valueObservable = new Observable<
        WrapWithTimestamp<OMap[keyof OMap]>
      >((subscriber) => {
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

  private getSingleTag<T extends "input" | "output">(
    type: T
  ): T extends "input" ? keyof IMap : keyof OMap {
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
          conns: defG.getInboundNodeSets(specNodeId).results.map((s) => ({
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
          conns: defG.getOutboundNodeSets(specNodeId).results.map((s) => ({
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

    const pass1 = pass0.filter(({ conns }) => conns.length === 1);
    const pass2 = pass1
      .filter(({ conns }) => {
        const conn = conns[0];
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
        conns: qualified.conns.map((c) => ({
          ...c,
          alias: this.convertSpecTagToWorkflowAlias(c),
        })),
      }));
    const qualified = pass2;
    if (qualified.length === 0) {
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
  public defineWorker<WP extends object | undefined>(
    p: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec">
  ) {
    return new ZZWorkerDef<P, I, O, WP, IMap, OMap>({
      ...p,
      jobSpec: this,
    });
  }

  public async defineWorkerAndStart<WP extends object | undefined>({
    instanceParams,
    ...p
  }: Omit<ZZWorkerDefParams<P, I, O, WP, IMap, OMap>, "jobSpec"> & {
    instanceParams?: WP;
  }) {
    const workerDef = this.defineWorker(p);
    await workerDef.startWorker({ instanceParams });
    return workerDef;
  }

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
  }

  submitAndWait: <KI extends keyof IMap, KO extends keyof OMap>(p: {
    input: IMap[KI];
    inputTag?: KI;
    outputTag?: KO;
  }) => Promise<{ data: OMap[KO]; timestamp: number } | null> = async (p) => {
    const { input: inputData, inputTag, outputTag } = p;
    await this.input.feed(inputData, inputTag);
    return await this.output(outputTag).nextValue();
  };
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
  nextValue: () => Promise<WrapWithTimestamp<OMap[keyof OMap]> | null>;
  mostRecentValue: () => Promise<WrapWithTimestamp<OMap[keyof OMap]> | null>;
  valueObservable: Observable<WrapWithTimestamp<OMap[keyof OMap]> | null>;

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
export function resolveTagMapping(
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
