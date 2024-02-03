// import { InferInputType, InferOutputType } from "../jobs/ZZJobSpec";
import { CheckSpec, SpecOrName, ZZJobSpec } from "../jobs/ZZJobSpec";
import { IOSpec, InferTMap } from "@livestack/shared/IOSpec";
import { z } from "zod";
import { ZZEnv } from "../jobs/ZZEnv";
import _ from "lodash";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { JobsOptions } from "bullmq";
import { JobNode, InstantiatedGraph, CanonicalConnection } from "./Graph";
import {
  SpecAndOutlet,
  resolveUniqueSpec,
  resolveTagMapping,
} from "../jobs/ZZJobSpec";

type SpecAndOutletOrTagged = SpecAndOutlet | TagObj<any, any, any, any, any>;

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

export type JobSpecAndJobOptions<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobOptions: z.infer<CheckSpec<JobSpec>["jobOptions"]>;
  jobLabel?: string;
};

type WorkflowParams = {
  connections: (
    | {
        from: SpecAndOutletOrTagged;
        to: SpecAndOutletOrTagged;
      }
    | SpecAndOutletOrTagged[]
  )[];
};

const WorkflowChildJobOptions = z.array(
  z.object({
    spec: SpecOrName,
    params: z.any(),
  })
);
type WorkflowChildJobOptions = z.infer<typeof WorkflowChildJobOptions>;
const WorkflowChildJobOptionsSanitized = z.array(
  z.object({
    spec: z.string(),
    params: z.any(),
  })
);

type WorkflowChildJobOptionsSanitized = z.infer<
  typeof WorkflowChildJobOptionsSanitized
>;

const WorkflowJobOptionsSanitized = z.object({
  groupId: z.string(),
  jobOptions: WorkflowChildJobOptionsSanitized.optional(),
});
type WorkflowJobOptionsSanitized = z.infer<typeof WorkflowJobOptionsSanitized>;

export class ZZWorkflowSpec extends ZZJobSpec<
  WorkflowChildJobOptionsSanitized,
  any,
  {
    status: z.ZodType<{
      status: "finished";
    }>;
  },
  any,
  any
> {
  public readonly connections: CanonicalConnection[];
  private workflowGraphCreated: boolean = false;

  public override getDefGraph() {
    const g = super.getDefGraph();
    if (!this.workflowGraphCreated) {
      const parentSpecNodeId = g.getRootSpecNodeId();
      if (!parentSpecNodeId) {
        throw new Error("No parent spec node id found");
      }

      for (const conn of this.connections) {
        const { fromSpecNodeId, toSpecNodeId } = g.addConnectedDualSpecs(
          conn[0],
          conn[1]
        );
        g.ensureParentChildRelation(parentSpecNodeId, fromSpecNodeId);
        g.ensureParentChildRelation(parentSpecNodeId, toSpecNodeId);
      }

      this.workflowGraphCreated = true;
    }
    return g;
  }

  private orchestrationWorkerDef: ZZWorkerDef<
    WorkflowChildJobOptionsSanitized,
    any,
    any,
    any,
    any,
    any
  >;

  protected readonly inputSpecTagByWorkflowTag: Record<
    string,
    { specName: string; tag: string; uniqueSpecLabel?: string }
  >;
  protected readonly outputSpecTagByWorkflowTag: Record<
    string,
    { specName: string; tag: string; uniqueSpecLabel?: string }
  >;
  protected readonly publicTagBySpecUniqueLabelAndTag: Record<
    `${string}[${string | ""}]::${"in" | "out"}/${string}`,
    string
  >;

  constructor({
    connections,
    name,
    zzEnv,
  }: {
    name: string;
    zzEnv?: ZZEnv;
  } & WorkflowParams) {
    super({
      name,
      jobOptions: WorkflowChildJobOptionsSanitized,
      zzEnv,
      output: {
        status: z.object({ status: z.literal("finished") }),
      },
    });
    const canonicalConns = convertConnectionsCanonical({
      connections,
    });
    this.connections = canonicalConns;

    // collect all the input and output that are tagged
    let inputSpecTagByWorkflowTag: Record<
      string,
      { specName: string; tag: string }
    > = {};
    let outputSpecTagByWorkflowTag: Record<
      string,
      {
        specName: string;
        tag: string;
      }
    > = {
      status: {
        specName: this.name,
        tag: "status",
      },
    };

    // reverse
    let publicTagBySpecUniqueLabelAndTag: Record<
      `${string}[${string | ""}]::${"in" | "out"}/${string}`,
      string
    > = {};

    for (const conn of canonicalConns) {
      for (const c of conn) {
        inputSpecTagByWorkflowTag = {
          ...inputSpecTagByWorkflowTag,
          ..._.fromPairs(
            _.toPairs(c.inputTagMap).map(([specTag, wfTag]) => [
              wfTag,
              {
                specName: c.spec.name,
                tag: specTag,
                uniqueSpecLabel: c.uniqueSpecLabel,
              },
            ])
          ),
        };
        outputSpecTagByWorkflowTag = {
          ...outputSpecTagByWorkflowTag,
          ..._.fromPairs(
            _.toPairs(c.outputTagMap).map(([specTag, wfTag]) => [
              wfTag,
              {
                specName: c.spec.name,
                tag: specTag,
                uniqueSpecLabel: c.uniqueSpecLabel,
              },
            ])
          ),
        };
        for (const [specTag, tag] of Object.entries(c.inputTagMap)) {
          publicTagBySpecUniqueLabelAndTag[
            `${c.spec.name}[${c.uniqueSpecLabel || ""}]::in/${specTag}`
          ] = tag;
        }
        for (const [specTag, tag] of Object.entries(c.outputTagMap)) {
          publicTagBySpecUniqueLabelAndTag[
            `${c.spec.name}[${c.uniqueSpecLabel || ""}]::out/${specTag}`
          ] = tag;
        }
      }
    }
    this.inputSpecTagByWorkflowTag = inputSpecTagByWorkflowTag;
    this.outputSpecTagByWorkflowTag = outputSpecTagByWorkflowTag;
    this.publicTagBySpecUniqueLabelAndTag = publicTagBySpecUniqueLabelAndTag;

    this._validateConnections();
    // for (const conn of canonicalConns) {
    //   this.getDefGraph().addConnectedDualSpecs(conn[0], conn[1]);
    // }
    this.orchestrationWorkerDef = new ZZWorkerDef({
      jobSpec: this,
      processor: async ({ jobOptions: childrenJobOptions, jobId, output }) => {
        const groupId = jobId;

        const instG = new InstantiatedGraph({
          defGraph: this.getDefGraph(),
          contextId: groupId,
          rootJobId: groupId,
          streamIdOverrides: {},
        });

        const allJobNodes = instG
          .nodes()
          .filter((n) => instG.getNodeAttributes(n).nodeType === "job");
        // remove self
        const jobNodesExceptSelf = allJobNodes.filter((n) => {
          const node = instG.getNodeAttributes(n) as JobNode;
          return node.jobId !== `[${groupId}]${this.name}`;
        });

        for (let i = 0; i < jobNodesExceptSelf.length; i++) {
          const jobNodeId = jobNodesExceptSelf[i];
          const jobNode = instG.getNodeAttributes(jobNodeId) as JobNode;

          //calculate input and output overrides
          const inputStreamIdOverridesByTag: Record<string, string> = {};
          const outputStreamIdOverridesByTag: Record<string, string> = {};

          // get the stream id overrides for the input
          const inboundEdges = instG.inboundEdges(jobNodeId);
          const inletEdgeIds = inboundEdges.filter((e) => {
            const node = instG.getNodeAttributes(instG.source(e));
            return node.nodeType === "inlet";
          });
          const inletNodeIds = inletEdgeIds.map((e) => instG.source(e));
          for (const inletNodeId of inletNodeIds) {
            const inletNode = instG.getNodeAttributes(inletNodeId);
            if (inletNode.nodeType !== "inlet") {
              throw new Error("Expected inlet node");
            }
            const streamToInpetEdgeId = instG.inboundEdges(inletNodeId)[0];
            const streamNodeId = instG.source(streamToInpetEdgeId);
            const streamNode = instG.getNodeAttributes(streamNodeId);
            if (streamNode.nodeType !== "stream") {
              throw new Error("Expected stream node");
            }
            const streamId = streamNode.streamId;
            inputStreamIdOverridesByTag[inletNode.tag] = streamId;
          }

          // get the stream id overrides for the output
          const outboundEdges = instG.outboundEdges(jobNodeId);
          const outletEdgeIds = outboundEdges.filter((e) => {
            const node = instG.getNodeAttributes(instG.target(e));
            return node.nodeType === "outlet";
          });
          const outletNodeIds = outletEdgeIds.map((e) => instG.target(e));

          for (const outletNodeId of outletNodeIds) {
            const outletNode = instG.getNodeAttributes(outletNodeId);
            if (outletNode.nodeType !== "outlet") {
              throw new Error("Expected outlet node");
            }

            const streamFromOutputEdgeId = instG.outboundEdges(outletNodeId)[0];
            const streamNodeId = instG.target(streamFromOutputEdgeId);
            const streamNode = instG.getNodeAttributes(streamNodeId);
            if (streamNode.nodeType !== "stream") {
              throw new Error("Expected stream node");
            }
            const streamId = streamNode.streamId;
            outputStreamIdOverridesByTag[outletNode.tag] = streamId;
          }

          const childSpecName = jobNode.specName;
          const childJobSpec = ZZJobSpec.lookupByName(childSpecName);

          await childJobSpec.enqueueJob({
            jobId: jobNode.jobId,
            parentJobId: groupId,
            uniqueSpecLabel: jobNode.uniqueSpecLabel,
            jobOptions: childrenJobOptions?.find(({ spec: specQuery }) => {
              const specInfo = resolveUniqueSpec(specQuery);
              return (
                specInfo.spec.name === childSpecName &&
                specInfo.uniqueSpecLabel === jobNode.uniqueSpecLabel
              );
            })?.params,
            inputStreamIdOverridesByTag,
            outputStreamIdOverridesByTag,
          });
        }
        await output("status").emit({ status: "finished" });
      },
    });
  }

  protected override convertSpecTagToPublicTag({
    specName,
    tag,
    uniqueSpecLabel,
    type,
  }: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
  }) {
    return this.publicTagBySpecUniqueLabelAndTag[
      `${specName}[${uniqueSpecLabel || ""}]::${type}/${tag}`
    ];
  }

  protected override convertPublicTagToSpecTag({
    type,
    tag,
  }: {
    type: "in" | "out";
    tag: string | symbol | number;
  }) {
    if (type === "in") {
      if (!this.inputSpecTagByWorkflowTag[tag.toString()]) {
        throw new Error(
          `No input spec tag found in workflow ${
            this.name
          } for ${tag.toString()}`
        );
      }
      return {
        ...this.inputSpecTagByWorkflowTag[tag.toString()],
        type,
      };
    } else {
      if (!this.outputSpecTagByWorkflowTag[tag.toString()]) {
        throw new Error(
          `No output spec tag found in workflow ${
            this.name
          } for ${tag.toString()}`
        );
      }
      return {
        ...this.outputSpecTagByWorkflowTag[tag.toString()],
        type,
      };
    }
  }

  private _validateConnections() {
    // calculate overrides based on jobConnectors
    for (let i = 0; i < this.connections.length; i++) {
      for (let j = 0; j < this.connections[i].length - 1; j++) {
        const outSpecInfo = this.connections[i][j];
        const inSpecInfo = this.connections[i][j + 1];
        validateSpecHasKey({
          spec: outSpecInfo.spec,
          type: "out",
          tag: outSpecInfo.tagInSpec,
        });
        validateSpecHasKey({
          spec: inSpecInfo.spec,
          type: "in",
          tag: inSpecInfo.tagInSpec,
        });
      }

      // TODO: to bring back this check
      // const fromDef = fromJobDecs.spec.outputDefSet.getDef(fromKeyStr);
      // const toDef = toJobDesc.spec.inputDefSet.getDef(toKeyStr);
      // if (hashDef(fromDef) !== hashDef(toDef)) {
      //   const msg = `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`;
      //   console.error(
      //     msg,
      //     "Upstream schema: ",
      //     zodToJsonSchema(fromDef),
      //     "Downstream schema: ",
      //     zodToJsonSchema(toDef)
      //   );
      //   throw new Error(msg);
      // }
      // validate that the types match
    }
  }

  protected override async lookUpChildJobIdByGroupIDAndSpecTag({
    groupId,
    specInfo,
  }: {
    groupId: string;
    specInfo: {
      specName: string;
      uniqueSpecLabel?: string;
    };
  }) {
    if (!this.zzEnvEnsured.db) {
      throw new Error("No db connection found");
    }

    let workflow = ZZWorkflow.lookupById(groupId);
    if (!workflow) {
      workflow = new ZZWorkflow({
        jobGroupId: groupId,
        jobGroupDef: this,
      });
    }

    const instaG = workflow.graph;

    const childJobNodeId = instaG.findNode((n) => {
      const node = instaG.getNodeAttributes(n);
      if (node.nodeType !== "job" && node.nodeType !== "root-job") {
        return false;
      } else {
        return (
          node.specName === specInfo.specName &&
          (node.nodeType === "root-job" ||
            (node.uniqueSpecLabel || null) ===
              (specInfo.uniqueSpecLabel || null))
        );
      }
    });
    if (!childJobNodeId) {
      throw new Error(
        `No child job found for ${groupId} and ${specInfo.specName}`
      );
    }
    return (instaG.getNodeAttributes(childJobNodeId) as JobNode).jobId;
  }

  public async startWorker(
    p?: Parameters<typeof this.orchestrationWorkerDef.startWorker>[0]
  ) {
    await this.orchestrationWorkerDef.startWorker(p);
    return this;
  }

  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: WorkflowChildJobOptionsSanitized;
    bullMQJobsOpts?: JobsOptions;
  }) {
    let { jobOptions } = p || {};

    // sanitize child job options
    const childJobOptionsSanitized = (jobOptions || []).map((c) => ({
      spec: typeof c.spec === "string" ? c.spec : c.spec,
      params: c.params,
    }));

    const manager = await super.enqueueJob({
      ...p,
      jobOptions: childJobOptionsSanitized,
    });

    const out = await manager.output("status");
    console.log("e");
    // wait on output to finish
    const r = await out.nextValue();
    console.log("r", r);
    return manager;
  }
}

export class ZZWorkflow {
  private _graph: InstantiatedGraph | null = null;
  public get graph(): InstantiatedGraph {
    if (!this._graph) {
      this._graph = new InstantiatedGraph({
        defGraph: this.jobGroupDef.getDefGraph(),
        contextId: this.contextId,
        rootJobId: this.contextId,
        streamIdOverrides: {},
      });
    }
    return this._graph;
  }
  public readonly jobGroupDef: ZZWorkflowSpec;
  public readonly contextId: string;
  private static _workflowById: Record<string, ZZWorkflow> = {};

  constructor({
    jobGroupDef,
    jobGroupId,
  }: {
    jobGroupId: string;
    jobGroupDef: ZZWorkflowSpec;
  }) {
    this.contextId = jobGroupId;
    this.jobGroupDef = jobGroupDef;
    ZZWorkflow._workflowById[jobGroupId] = this;
  }

  public static define(p: ConstructorParameters<typeof ZZWorkflowSpec>[0]) {
    return new ZZWorkflowSpec(p);
  }

  public static lookupById(jobGroupId: string) {
    const r = this._workflowById[jobGroupId] as ZZWorkflow | undefined;
    return r || null;
  }
}

function convertSpecAndOutletWithTags(
  specAndOutletOrTagged: SpecAndOutletOrTagged
): {
  spec: ZZJobSpec<any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec?: string;
  inputTagMap: Record<string, string>;
  outputTagMap: Record<string, string>;
} {
  if (Array.isArray(specAndOutletOrTagged)) {
    const [uniqueSpec, tagInSpec] = specAndOutletOrTagged;
    const uniqueSpecLabel = resolveUniqueSpec(uniqueSpec).uniqueSpecLabel;
    const tagMaps = resolveTagMapping(uniqueSpec);
    return {
      spec: resolveUniqueSpec(uniqueSpec).spec,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      tagInSpec,
      ...tagMaps,
    };
  } else {
    const converted = resolveUniqueSpec(specAndOutletOrTagged);
    const uniqueSpecLabel = converted.uniqueSpecLabel;
    const tagMaps = resolveTagMapping(specAndOutletOrTagged);

    return {
      spec: converted.spec,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      ...tagMaps,
    };
  }
}

export type CanonicalSpecAndOutlet = ReturnType<
  typeof convertSpecAndOutletWithTags
> & {
  tagInSpec: string;
  tagInSpecType: "input" | "output";
};

function convertConnectionsCanonical(workflowParams: WorkflowParams) {
  const convertedConnections = workflowParams.connections.reduce(
    (acc, conn) => {
      if (Array.isArray(conn)) {
        let newAcc: [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet][] = [];
        const connCanonical = conn.map(convertSpecAndOutletWithTags);
        for (let i = 0; i < connCanonical.length - 1; i++) {
          newAcc.push([
            {
              ...connCanonical[i],
              tagInSpecType: "output",
              tagInSpec:
                connCanonical[i].tagInSpec ||
                String(connCanonical[i].spec.getSingleOutputTag()),
            },
            {
              ...connCanonical[i + 1],
              tagInSpecType: "input",
              tagInSpec:
                connCanonical[i + 1].tagInSpec ||
                String(connCanonical[i + 1].spec.getSingleInputTag()),
            },
          ]);
        }
        return newAcc;
      } else {
        return [
          ...acc,
          [
            {
              ...convertSpecAndOutletWithTags(conn.from),
              tagInSpecType: "output",
            },
            {
              ...convertSpecAndOutletWithTags(conn.to),
              tagInSpecType: "input",
            },
          ] as [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet],
        ];
      }
    },
    [] as [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet][]
  );

  return convertedConnections;
}

function validateSpecHasKey<P, I, O, IMap, OMap>({
  spec,
  type,
  tag,
}: {
  spec: IOSpec<I, O, IMap, OMap>;
  type: "in" | "out";
  tag: string;
}) {
  if (type === "in") {
    if (!spec.outputDefSet.hasDef(tag)) {
      throw new Error(`Invalid spec tag: ${spec.name}/${tag} specified.`);
    }
  }
}

export type TagMaps<I, O, IKs, OKs> = {
  inputTag: Partial<Record<keyof InferTMap<I>, IKs>>;
  outputTag: Partial<Record<keyof InferTMap<O>, OKs>>;
};

export interface TagObj<P, I, O, IKs, OKs> {
  spec: ZZJobSpec<P, I, O>;
  input: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof InferTMap<I>, newK>>
  ) => TagObj<P, I, O, IKs | newK, OKs>;
  output: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof InferTMap<O>, newK>>
  ) => TagObj<P, I, O, IKs, OKs | newK>;
  _tagMaps: TagMaps<I, O, IKs, OKs>;
}

export function tag<P, IMap, OMap>(spec: ZZJobSpec<P, IMap, OMap>) {
  return _tagObj(spec, {
    inputTag: {},
    outputTag: {},
  } as TagMaps<IMap, OMap, never, never>);
}

function _tagObj<P, I, O, IKs, OKs>(
  spec: ZZJobSpec<P, I, O>,
  _tagMaps: TagMaps<I, O, IKs, OKs>
): TagObj<P, I, O, IKs, OKs> {
  const tagMaps = { ..._tagMaps };
  return {
    spec,
    _tagMaps: tagMaps,
    input: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof InferTMap<I>, Ks>>
    ) => {
      const tm = tagMaps as TagMaps<I, O, IKs | Ks, OKs>;
      if (typeof tagOrMap === "string") {
        const key = spec.getSingleInputTag();
        tm.inputTag[key] = tagOrMap;
      } else {
        tm.inputTag = { ...tagMaps.inputTag, ...tagOrMap };
      }
      return _tagObj(spec, tm);
    },
    output: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof InferTMap<O>, string>>
    ) => {
      const tm = tagMaps as TagMaps<I, O, IKs, OKs | Ks>;
      if (typeof tagOrMap === "string") {
        const tag = spec.getSingleOutputTag();
        tm.outputTag[tag] = tagOrMap;
      } else {
        tm.outputTag = { ...tagMaps.outputTag, ...tagOrMap };
      }
      return _tagObj(spec, tm);
    },
  };
}
