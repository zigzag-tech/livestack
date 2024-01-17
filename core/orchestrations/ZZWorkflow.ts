// import { InferInputType, InferOutputType } from "../jobs/ZZJobSpec";
import {
  CheckSpec,
  deriveStreamId,
  uniqueStreamIdentifier,
  ZZJobSpec,
} from "../jobs/ZZJobSpec";
import { z } from "zod";
import Graph from "graphology";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { v4 } from "uuid";
import { ZZEnv } from "../jobs/ZZEnv";
import { SpecAndOutlet, UniqueSpecQuery } from "../jobs/ZZJobSpec";
import { SpecOrName } from "@livestack/shared/ZZJobSpecBase";
import { resolveUniqueSpec } from "../jobs/ZZJobSpec";

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

export type JobSpecAndJobParams<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobParams: z.infer<CheckSpec<JobSpec>["jobParams"]>;
  jobLabel?: string;
};

const WorkflowParams = z.object({
  connections: z.array(
    z
      .object({
        from: SpecAndOutlet,
        to: SpecAndOutlet,
      })
      .or(z.array(SpecAndOutlet))
  ),
});
type WorkflowParams = z.infer<typeof WorkflowParams>;

type CanonicalWorkflowParams = ReturnType<typeof convertConnectionsCanonical>;

type CanonicalConnection = CanonicalWorkflowParams[number];
type CanonicalConnectionPoint = CanonicalConnection[number];

const WorkflowChildJobParams = z.array(
  z.object({
    spec: SpecOrName,
    params: z.any(),
  })
);
type WorkflowChildJobParams = z.infer<typeof WorkflowChildJobParams>;
const WorkflowJobParams = z.object({
  groupId: z.string(),
  jobParams: WorkflowChildJobParams.optional(),
});

type WorkflowJobParams = z.infer<typeof WorkflowJobParams>;
export class ZZWorkflowSpec extends ZZJobSpec<WorkflowJobParams> {
  public readonly connections: CanonicalConnection[];
  public readonly defGraph: DefGraph;
  private orchestrationWorkerDef: ZZWorkerDef<WorkflowJobParams>;

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
      jobParams: WorkflowJobParams,
      zzEnv,
    });
    const canonical = convertConnectionsCanonical({
      connections,
    });
    this.connections = canonical;
    this._validateConnections();
    this.defGraph = convertedConnectionsToGraph(canonical);
    this.orchestrationWorkerDef = new ZZWorkerDef({
      jobSpec: this,
      processor: async ({
        jobParams: { groupId, jobParams: childrenJobParams },
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
          const inputStreamIdOverridesByKey: Record<string, string> = {};
          const outputStreamIdOverridesByKey: Record<string, string> = {};

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
            inputStreamIdOverridesByKey[inletNode.key] = streamId;
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
            outputStreamIdOverridesByKey[outletNode.key] = streamId;
          }

          const childSpecName = jobNode.specName;
          const childJobSpec = ZZJobSpec.lookupByName(childSpecName);
          await childJobSpec.enqueueJob({
            jobId: jobNode.jobId,
            jobParams: childrenJobParams?.find(({ spec: specQuery }) => {
              const specInfo = resolveUniqueSpec(specQuery);
              return (
                specInfo.specName === childSpecName &&
                specInfo.uniqueLabel === jobNode.uniqueLabel
              );
            })?.params,
            inputStreamIdOverridesByKey,
            outputStreamIdOverridesByKey,
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
          spec: ZZJobSpec.lookupByName(outSpecInfo.specName),
          type: "out",
          key: outSpecInfo.key,
        });
        validateSpecHasKey({
          spec: ZZJobSpec.lookupByName(inSpecInfo.specName),
          type: "in",
          key: inSpecInfo.key,
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
    jobParams: childJobParams,
  }: // lazyJobCreation = false,
  {
    jobGroupId?: string;
    // lazyJobCreation?: boolean;
    jobParams?: WorkflowChildJobParams;
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
      jobParams: {
        groupId: jobGroupId,
        jobParams: childJobParams,
      },
    });

    return workflow;
  }
}

export class ZZWorkflow {
  public readonly jobIdBySpec: (specQuery: UniqueSpecQuery) => string;
  public readonly graph: InstantiatedGraph;
  public readonly input: {
    bySpec: (
      spec: UniqueSpecQuery
    ) => ReturnType<ZZJobSpec<any, any, any>["_deriveInputsForJob"]>;
  };
  public readonly output: {
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
          n.specName === specInfo.specName &&
          n.uniqueLabel === specInfo.uniqueLabel
        );
      });

      if (!jobNodeId) {
        throw new Error(
          `Job of spec ${specInfo.specName} ${
            specInfo.uniqueLabel ? `with label ${specInfo.uniqueLabel}` : ""
          } not found.`
        );
      }
      const jobId = (instaG.getNodeAttributes(jobNodeId) as JobNode).jobId;
      const childSpec = ZZJobSpec.lookupByName(specInfo.specName);

      return { spec: childSpec, jobId };
    };

    const jobIdBySpec = (specQuery: UniqueSpecQuery) => {
      const { jobId } = identifySpecAndJobIdBySpecQuery(specQuery);
      return jobId;
    };

    const input = {
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveInputsForJob(jobId);
      },
    };

    const output = {
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveOutputsForJob(jobId);
      },
    };

    this.jobIdBySpec = jobIdBySpec;
    this.input = input;
    this.output = output;
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

// Conversion functions using TypeScript
export function convertSpecOrName(specOrName: SpecOrName): string {
  if (typeof specOrName === "string") {
    return specOrName;
  } else {
    return specOrName.name;
  }
}

function convertSpecAndOutlet(specAndOutlet: SpecAndOutlet): {
  specName: string;
  uniqueLabel?: string;
  key: string;
} {
  if (Array.isArray(specAndOutlet)) {
    const [uniqueSpec, key] = specAndOutlet;
    const uniqueLabel = resolveUniqueSpec(uniqueSpec).uniqueLabel;
    return {
      specName: resolveUniqueSpec(uniqueSpec).specName,
      ...(uniqueLabel ? { uniqueLabel } : {}),
      key: key,
    };
  } else {
    const converted = resolveUniqueSpec(specAndOutlet);
    const uniqueLabel = converted.uniqueLabel;
    return {
      specName: converted.specName,
      ...(uniqueLabel ? { uniqueLabel } : {}),
      key: "default",
    };
  }
}

type CanonicalSpecAndOutlet = ReturnType<typeof convertSpecAndOutlet>;

function convertConnectionsCanonical(workflowParams: WorkflowParams) {
  const convertedConnections = workflowParams.connections.reduce(
    (acc, conn) => {
      if (Array.isArray(conn)) {
        let newAcc: [CanonicalSpecAndOutlet, CanonicalSpecAndOutlet][] = [];
        const connCanonical = conn.map(convertSpecAndOutlet);
        for (let i = 0; i < connCanonical.length - 1; i++) {
          newAcc.push([connCanonical[i], connCanonical[i + 1]]);
        }
        return newAcc;
      } else {
        return [
          ...acc,
          [convertSpecAndOutlet(conn.from), convertSpecAndOutlet(conn.to)] as [
            CanonicalSpecAndOutlet,
            CanonicalSpecAndOutlet
          ],
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
  uniqueLabel?: string;
};
type OutletNode = {
  nodeType: "outlet";
  key: string;
};
type InletNode = {
  nodeType: "inlet";
  key: string;
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
  uniqueLabel?: string;
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
        key: string;
      }
    | {
        nodeType: "outlet";
        key: string;
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
    const fromUniqueLabel = from.uniqueLabel;
    const fromSpecNodeId = createOrGetNodeId(fromSpecIdentifier, {
      specName: from.specName,
      ...(fromUniqueLabel ? { uniqueLabel: fromUniqueLabel } : {}),
      nodeType: "spec",
      label: fromSpecIdentifier,
    });
    const fromOutletNodeId = createOrGetNodeId(
      `${fromSpecIdentifier}/${from.key}`,
      {
        nodeType: "outlet",
        key: from.key,
        label: `${fromSpecIdentifier}/${from.key}`,
      }
    );
    const streamId = uniqueStreamIdentifier({
      from: {
        specName: from.specName,
        uniqueLabel: from.uniqueLabel,
        key: from.key,
      },
      to: {
        specName: to.specName,
        uniqueLabel: to.uniqueLabel,
        key: to.key,
      },
    });
    const streamNodeId = createOrGetNodeId(streamId, {
      nodeType: "stream",
      label: streamId,
    });
    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const id = `${toSpecIdentifier}/${to.key}`;
    const toInletNodeId = createOrGetNodeId(id, {
      nodeType: "inlet",
      key: to.key,
      label: id,
    });
    const toUniqueLabel = to.uniqueLabel;
    const toSpecNodeId = createOrGetNodeId(toSpecIdentifier, {
      specName: to.specName,
      ...(toUniqueLabel ? { uniqueLabel: toUniqueLabel } : {}),
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
  uniqueLabel,
}: {
  specName: string;
  uniqueLabel?: string;
}) {
  return `${specName}${uniqueLabel ? `[${uniqueLabel}]` : ""}`;
}

function validateSpecHasKey<P, IMap, OMap>({
  spec,
  type,
  key,
}: {
  spec: ZZJobSpec<P, IMap, OMap>;
  type: "in" | "out";
  key: string;
}) {
  if (type === "in") {
    if (!spec.outputDefSet.hasDef(key)) {
      throw new Error(`Invalid spec key: ${spec.name}/${key} specified.`);
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
          uniqueLabel: sourceSpecNode.uniqueLabel,
          key: outletNode.key,
        },
        to: {
          specName: targetSpecNode.specName,
          uniqueLabel: targetSpecNode.uniqueLabel,
          key: inletNode.key,
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
