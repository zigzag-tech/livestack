// import { InferInputType, InferOutputType } from "../jobs/ZZJobSpec";
import { v4 } from "uuid";
import {
  CheckSpec,
  deriveStreamId,
  uniqueStreamIdentifier,
  ZZJobSpec,
} from "../jobs/ZZJobSpec";
import { z } from "zod";
import Graph from "graphology";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { ZZEnv } from "../jobs/ZZEnv";
import { InferDefMap } from "@livestack/shared/StreamDefSet";

type SpecAndOutletOrTagged = SpecAndOutlet | TagObj<any, any, any, any, any>;

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

export type JobSpecAndJobOptions<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobOptions: z.infer<CheckSpec<JobSpec>["jobOptions"]>;
  jobLabel?: string;
};

// const WorkflowParams = z.object({
//   connections: z.array(
//     z
//       .object({
//         from: SpecAndOutlet,
//         to: SpecAndOutlet,
//       })
//       .or(z.array(SpecAndOutlet))
//   ),
// });
type WorkflowParams = {
  connections: (
    | {
        from: SpecAndOutletOrTagged;
        to: SpecAndOutletOrTagged;
      }
    | SpecAndOutletOrTagged[]
  )[];
};

type CanonicalWorkflowParams = ReturnType<typeof convertConnectionsCanonical>;

type CanonicalConnection = CanonicalWorkflowParams[number];
type CanonicalConnectionPoint = CanonicalConnection[number];
export const SpecOrName = z.union([
  z.string(),
  z.instanceof(ZZJobSpec<any, any, any>),
]);
export type SpecOrName = z.infer<typeof SpecOrName>; // Conversion functions using TypeScript

const WorkflowChildJobOptions = z.array(
  z.object({
    spec: SpecOrName,
    params: z.any(),
  })
);
type WorkflowChildJobOptions = z.infer<typeof WorkflowChildJobOptions>;
const WorkflowJobOptions = z.object({
  groupId: z.string(),
  jobOptions: WorkflowChildJobOptions.optional(),
});

type WorkflowJobOptions = z.infer<typeof WorkflowJobOptions>;
export class ZZWorkflowSpec extends ZZJobSpec<WorkflowJobOptions, any, any> {
  public readonly connections: CanonicalConnection[];
  public readonly defGraph: DefGraph;
  private orchestrationWorkerDef: ZZWorkerDef<WorkflowJobOptions>;

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
      jobOptions: WorkflowJobOptions,
      zzEnv,
    });
    const canonicalConns = convertConnectionsCanonical({
      connections,
    });
    this.connections = canonicalConns;
    this._validateConnections();
    this.defGraph = convertedConnectionsToGraph(canonicalConns);
    this.orchestrationWorkerDef = new ZZWorkerDef({
      jobSpec: this,
      processor: async ({
        jobOptions: { groupId, jobOptions: childrenJobOptions },
      }) => {
        const instG = instantiateFromDefGraph({
          defGraph: this.defGraph,
          groupId,
        });

        const jobNodes = instG
          .nodes()
          .filter((n) => instG.getNodeAttributes(n).nodeType === "job");

        for (let i = 0; i < jobNodes.length; i++) {
          const jobNodeId = jobNodes[i];
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
      },
    });
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

  public async startWorker(
    p?: Parameters<typeof this.orchestrationWorkerDef.startWorker>[0]
  ) {
    await this.orchestrationWorkerDef.startWorker(p);
    return this;
  }

  public async enqueue({
    jobGroupId,
    jobOptions: childJobOptions,
  }: // lazyJobCreation = false,
  {
    jobGroupId?: string;
    // lazyJobCreation?: boolean;
    jobOptions?: WorkflowChildJobOptions;
  }) {
    if (!jobGroupId) {
      jobGroupId = v4();
    }

    // Create interfaces for input and output

    // console.log("countByName", countByName);
    const workflow = new ZZWorkflow({
      jobGroupDef: this,
      jobGroupId,
    });

    await this.enqueueJob({
      jobId: jobGroupId,
      jobOptions: {
        groupId: jobGroupId,
        jobOptions: childJobOptions,
      },
    });

    return workflow;
  }
}

export class ZZWorkflow {
  public readonly jobIdBySpec: (specQuery: UniqueSpecQuery) => string;
  public readonly graph: InstantiatedGraph;
  public readonly input: {
    byTag: (tag: string) => {
      feed: (data: any) => Promise<void>;
      terminate: () => Promise<void>;
    };
    bySpec: (
      spec: UniqueSpecQuery
    ) => ReturnType<ZZJobSpec<any, any, any>["_deriveInputsForJob"]>;
  };
  public readonly output: {
    byTag: (tag: string) => AsyncGenerator<{ data: any }>;
    bySpec: (
      spec: UniqueSpecQuery
    ) => ReturnType<ZZJobSpec<any, any, any>["_deriveOutputsForJob"]>;
  };
  public readonly jobGroupDef: ZZWorkflowSpec;
  constructor({
    jobGroupDef,
    jobGroupId,
  }: {
    jobGroupId: string;
    jobGroupDef: ZZWorkflowSpec;
  }) {
    const instaG = instantiateFromDefGraph({
      defGraph: jobGroupDef.defGraph,
      groupId: jobGroupId,
    });

    this.graph = instaG;

    const identifySpecAndJobIdBySpecQuery = (
      specQuery: UniqueSpecQuery
    ): { spec: ZZJobSpec<any, any, any>; jobId: string } => {
      const specInfo = resolveUniqueSpec(specQuery);
      const jobNodeId = instaG.findNode((id, n) => {
        return (
          n.nodeType === "job" &&
          n.specName === specInfo.spec.name &&
          n.uniqueSpecLabel === specInfo.uniqueSpecLabel
        );
      });

      if (!jobNodeId) {
        throw new Error(
          `Job of spec ${specInfo.spec.name} ${
            specInfo.uniqueSpecLabel
              ? `with label ${specInfo.uniqueSpecLabel}`
              : ""
          } not found.`
        );
      }
      const jobId = (instaG.getNodeAttributes(jobNodeId) as JobNode).jobId;
      const childSpec = ZZJobSpec.lookupByName(specInfo.spec.name);

      return { spec: childSpec, jobId };
    };

    const jobIdBySpec = (specQuery: UniqueSpecQuery) => {
      const { jobId } = identifySpecAndJobIdBySpecQuery(specQuery);
      return jobId;
    };

    this.jobIdBySpec = jobIdBySpec;
    this.input = {
      byTag: (tag: string) => {
        // TODO
      },
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveInputsForJob(jobId);
      },
    };
    this.output = {
      byTag: (tag: string) => {},
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveOutputsForJob(jobId);
      },
    };
    this.jobGroupDef = jobGroupDef;
  }

  public static define(p: ConstructorParameters<typeof ZZWorkflowSpec>[0]) {
    return new ZZWorkflowSpec(p);
  }

  public static connect = connect;
}

export function connect<
  Spec1,
  Spec2
  // K1 extends keyof CheckSpec<Spec1>["outputDefSet"]["defs"],
  // K2 extends keyof CheckSpec<Spec2>["inputDefSet"]["defs"]
>({
  from,
  to,
}: // transform,
{
  from: Spec1;
  to: Spec2;
  // transform?: (
  //   spec1Out: NonNullable<InferOutputType<Spec1, K1>>
  // ) => NonNullable<InferInputType<Spec2, K2>>;
}): {
  from: Spec1;
  to: Spec2;
  // transform?: (
  //   spec1Out: NonNullable<InferOutputType<Spec1, K1>>
  // ) => NonNullable<InferInputType<Spec2, K2>>;
} {
  return {
    from,
    to,
    // transform,
  };
}

function convertSpecAndOutletWithTags(
  specAndOutletOrTagged: SpecAndOutletOrTagged
): {
  spec: ZZJobSpec<any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec: string;
  inputTagMap: Partial<Record<string, string>>;
  outputTagMap: Partial<Record<string, string>>;
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
    const tagInSpec = String(
      getSingleTag(converted.spec.outputDefSet.defs, "output")
    );

    return {
      spec: converted.spec,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      tagInSpec,
      ...tagMaps,
    };
  }
}

type CanonicalSpecAndOutlet = ReturnType<typeof convertSpecAndOutletWithTags>;

function convertConnectionsCanonical(workflowParams: WorkflowParams) {
  const convertedConnections = workflowParams.connections.reduce(
    (acc, conn) => {
      if (Array.isArray(conn)) {
        let newAcc: [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet][] = [];
        const connCanonical = conn.map(convertSpecAndOutletWithTags);
        for (let i = 0; i < connCanonical.length - 1; i++) {
          newAcc.push([connCanonical[i], connCanonical[i + 1]]);
        }
        return newAcc;
      } else {
        return [
          ...acc,
          [
            convertSpecAndOutletWithTags(conn.from),
            convertSpecAndOutletWithTags(conn.to),
          ] as [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet],
        ];
      }
    },
    [] as [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet][]
  );

  return convertedConnections;
}

type SpecNode = {
  nodeType: "spec";
  specName: string;
  uniqueSpecLabel?: string;
};
type OutletNode = {
  nodeType: "outlet";
  tag: string;
};
type InletNode = {
  nodeType: "inlet";
  tag: string;
};
type DefGraphNode = { label: string } & (
  | SpecNode
  | {
      nodeType: "stream";
    }
  | InletNode
  | OutletNode
);

type InferNodeData<T extends DefGraphNode["nodeType"]> = Extract<
  DefGraphNode,
  { nodeType: T }
>;

type DefNodeType = DefGraphNode["nodeType"];
type DefGraph = Graph<DefGraphNode>;

type JobNode = {
  nodeType: "job";
  jobId: string;
  specName: string;
  uniqueSpecLabel?: string;
};

type InstantiatedGraph = Graph<
  { label: string } & (
    | JobNode
    | {
        nodeType: "stream";
        streamId: string;
      }
    | {
        nodeType: "inlet";
        tag: string;
      }
    | {
        nodeType: "outlet";
        tag: string;
      }
  )
>;

function convertedConnectionsToGraph(
  convertedConnections: CanonicalConnection[]
): DefGraph {
  const graph: DefGraph = new Graph();

  function createOrGetNodeId<T extends DefNodeType>(
    id: string,
    data: InferNodeData<T>
  ): string {
    const nodeId = `${data.nodeType}_${id}`;
    if (!graph.hasNode(nodeId)) {
      graph.addNode(nodeId, { ...data });
    }
    return nodeId;
  }

  function addConnection(
    from: CanonicalConnectionPoint,
    to: CanonicalConnectionPoint
  ) {
    const fromSpecIdentifier = uniqueSpecIdentifier(from);
    const fromUniqueLabel = from.uniqueSpecLabel;
    const fromSpecNodeId = createOrGetNodeId(fromSpecIdentifier, {
      specName: from.spec.name,
      ...(fromUniqueLabel ? { uniqueSpecLabel: fromUniqueLabel } : {}),
      nodeType: "spec",
      label: fromSpecIdentifier,
    });
    const fromOutletNodeId = createOrGetNodeId(
      `${fromSpecIdentifier}/${from.tagInSpec}`,
      {
        nodeType: "outlet",
        tag: from.tagInSpec,
        label: `${fromSpecIdentifier}/${from.tagInSpec}`,
      }
    );
    const streamId = uniqueStreamIdentifier({
      from: {
        specName: from.tagInSpec,
        uniqueSpecLabel: from.uniqueSpecLabel,
        tag: from.tagInSpec,
      },
      to: {
        specName: to.spec.name,
        uniqueSpecLabel: to.uniqueSpecLabel,
        tag: to.tagInSpec,
      },
    });
    const streamNodeId = createOrGetNodeId(streamId, {
      nodeType: "stream",
      label: streamId,
    });
    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const id = `${toSpecIdentifier}/${to.tagInSpec}`;
    const toInletNodeId = createOrGetNodeId(id, {
      nodeType: "inlet",
      tag: to.tagInSpec,
      label: id,
    });
    const toUniqueLabel = to.uniqueSpecLabel;
    const toSpecNodeId = createOrGetNodeId(toSpecIdentifier, {
      specName: to.spec.name,
      ...(toUniqueLabel ? { uniqueSpecLabel: toUniqueLabel } : {}),
      nodeType: "spec",
      label: toSpecIdentifier,
    });

    graph.addEdge(fromSpecNodeId, fromOutletNodeId);
    graph.addEdge(fromOutletNodeId, streamNodeId);
    graph.addEdge(streamNodeId, toInletNodeId);
    graph.addEdge(toInletNodeId, toSpecNodeId);
  }

  for (const connection of convertedConnections) {
    // Split into multiple connections
    for (let i = 0; i < connection.length - 1; i++) {
      addConnection(connection[i], connection[i + 1]);
    }
  }

  return graph;
}

function uniqueSpecIdentifier({
  specName,
  spec,
  uniqueSpecLabel,
}: {
  specName?: string;
  spec?: ZZJobSpec<any, any, any>;
  uniqueSpecLabel?: string;
}) {
  specName = specName ?? spec?.name;
  if (!specName) {
    throw new Error("specName or spec must be provided");
  }
  return `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`;
}

function validateSpecHasKey<P, IMap, OMap>({
  spec,
  type,
  tag,
}: {
  spec: ZZJobSpec<P, IMap, OMap>;
  type: "in" | "out";
  tag: string;
}) {
  if (type === "in") {
    if (!spec.outputDefSet.hasDef(tag)) {
      throw new Error(`Invalid spec tag: ${spec.name}/${tag} specified.`);
    }
  }
}

function instantiateFromDefGraph({
  defGraph,
  groupId,
}: {
  defGraph: DefGraph;
  groupId: string;
}): InstantiatedGraph {
  const g: InstantiatedGraph = new Graph();

  const nodes = defGraph.nodes();
  const jobNodeIdBySpecNodeId: { [k: string]: string } = {};
  const streamNodeIdByStreamId: { [k: string]: string } = {};

  for (const nodeId of nodes) {
    const node = defGraph.getNodeAttributes(nodeId);
    if (node.nodeType === "spec") {
      const jobId = `[${groupId}]${uniqueSpecIdentifier(node)}`;
      g.addNode(jobId, {
        ...node,
        nodeType: "job",
        jobId,
      });
      jobNodeIdBySpecNodeId[nodeId] = jobId;
    } else if (node.nodeType === "stream") {
      const { sourceSpecNode, targetSpecNode, outletNode, inletNode } =
        getStreamNodes(defGraph, nodeId);
      const streamId = deriveStreamId({
        groupId,
        from: {
          specName: sourceSpecNode.specName,
          uniqueSpecLabel: sourceSpecNode.uniqueSpecLabel,
          tag: outletNode.tag,
        },
        to: {
          specName: targetSpecNode.specName,
          uniqueSpecLabel: targetSpecNode.uniqueSpecLabel,
          tag: inletNode.tag,
        },
      });
      g.addNode(streamId, {
        nodeType: "stream",
        streamId,
        label: streamId,
      });
      streamNodeIdByStreamId[nodeId] = streamId;
    } else {
      g.addNode(nodeId, node);
    }
  }

  const edges = defGraph.edges();
  for (const edgeId of edges) {
    const from = defGraph.source(edgeId);
    const to = defGraph.target(edgeId);
    const fromNode = defGraph.getNodeAttributes(from);
    const toNode = defGraph.getNodeAttributes(to);

    const newFrom =
      fromNode.nodeType === "spec"
        ? jobNodeIdBySpecNodeId[from]
        : fromNode.nodeType === "stream"
        ? streamNodeIdByStreamId[from]
        : from;
    const newTo =
      toNode.nodeType === "spec"
        ? jobNodeIdBySpecNodeId[to]
        : toNode.nodeType === "stream"
        ? streamNodeIdByStreamId[to]
        : to;
    g.addEdge(newFrom, newTo);
  }

  return g;
}

function getStreamNodes(g: DefGraph, streamNodeId: string) {
  const [ie] = g.inboundEdges(streamNodeId);
  const outletNodeId = g.source(ie);
  const outletNode = g.getNodeAttributes(outletNodeId) as OutletNode;
  const [ie2] = g.inboundEdges(outletNodeId);
  const sourceSpecNodeId = g.source(ie2);
  const sourceSpecNode = g.getNodeAttributes(sourceSpecNodeId) as SpecNode;

  const [oe] = g.outboundEdges(streamNodeId);
  const inletNodeId = g.target(oe);
  const inletNode = g.getNodeAttributes(inletNodeId) as InletNode;
  const [oe2] = g.outboundEdges(inletNodeId);
  const targetSpecNodeId = g.target(oe2);
  const targetSpecNode = g.getNodeAttributes(targetSpecNodeId) as SpecNode;

  return {
    sourceSpecNode,
    targetSpecNode,
    outletNode,
    inletNode,
  };
}

type TagMaps<IMap, OMap, IKs, OKs> = {
  inputTag: Partial<Record<keyof IMap, IKs>>;
  outputTag: Partial<Record<keyof OMap, OKs>>;
};

interface TagObj<P, IMap, OMap, IKs, OKs> {
  spec: ZZJobSpec<P, IMap, OMap>;
  input: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof IMap, newK>>
  ) => TagObj<P, IMap, OMap, IKs | newK, OKs>;
  output: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof OMap, newK>>
  ) => TagObj<P, IMap, OMap, IKs, OKs | newK>;
  _tagMaps: TagMaps<IMap, OMap, IKs, OKs>;
}

export function tag<P, IMap, OMap>(spec: ZZJobSpec<P, IMap, OMap>) {
  return _tagObj(spec, {
    inputTag: {},
    outputTag: {},
  } as TagMaps<IMap, OMap, never, never>);
}

function _tagObj<P, IMap, OMap, IKs, OKs>(
  spec: ZZJobSpec<P, IMap, OMap>,
  _tagMaps: TagMaps<IMap, OMap, IKs, OKs>
): TagObj<P, IMap, OMap, IKs, OKs> {
  const tagMaps = { ..._tagMaps };
  return {
    spec,
    _tagMaps: tagMaps,
    input: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof IMap, Ks>>
    ) => {
      const tm = tagMaps as TagMaps<IMap, OMap, IKs | Ks, OKs>;
      if (typeof tagOrMap === "string") {
        const key = getSingleTag(spec.inputDefSet.defs, "input");
        tm.inputTag[key] = tagOrMap;
      } else {
        tm.inputTag = { ...tagMaps.inputTag, ...tagOrMap };
      }
      return _tagObj(spec, tm);
    },
    output: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof OMap, string>>
    ) => {
      const tm = tagMaps as TagMaps<IMap, OMap, IKs, OKs | Ks>;
      if (typeof tagOrMap === "string") {
        const key = getSingleTag(spec.outputDefSet.defs, "output");
        tm.outputTag[key] = tagOrMap;
      } else {
        tm.inputTag = { ...tagMaps.inputTag, ...tagOrMap };
      }
      return _tagObj(spec, tm);
    },
  };
}

export function getSingleTag<TMap>(
  defSet: InferDefMap<TMap>,
  type: "input" | "output"
) {
  const keys = Object.keys(defSet);
  if (keys.length !== 1) {
    throw new Error(
      `Expected exactly one tag in the defintion of ${type}, found ${keys.length}.`
    );
  }
  return keys[0] as keyof TMap;
}

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

export function convertSpecOrName(specOrName: SpecOrName) {
  if (typeof specOrName === "string") {
    return ZZJobSpec.lookupByName(specOrName);
  } else {
    return specOrName;
  }
}

function resolveTagMapping({
  inputTag,
  outputTag,
}: Partial<TagMaps<any, any, any, any>> | any) {
  return {
    inputTagMap: inputTag ?? {},
    outputTagMap: outputTag ?? {},
  };
}
