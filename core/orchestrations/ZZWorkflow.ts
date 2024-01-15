// import { InferInputType, InferOutputType } from "../jobs/ZZJobSpec";
import {
  CheckSpec,
  deriveStreamId,
  uniqueStreamIdentifier,
  ZZJobSpec,
} from "../jobs/ZZJobSpec";
import { any, z } from "zod";

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

export type JobSpecAndJobParams<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobParams: z.infer<CheckSpec<JobSpec>["jobParams"]>;
  jobLabel?: string;
};

const SpecOrName = z.union([
  z.string(),
  z.instanceof(ZZJobSpec<any, any, any>),
]);
type SpecOrName = z.infer<typeof SpecOrName>;

const UniqueSpecQuery = z.union([
  SpecOrName,
  z.object({
    spec: SpecOrName,
    label: z.string().default("default_label"),
  }),
]);
type UniqueSpecQuery = z.infer<typeof UniqueSpecQuery>;

const SpecAndOutlet = z.union([
  UniqueSpecQuery,
  z.tuple([UniqueSpecQuery, z.string().or(z.literal("default"))]),
]);
type SpecAndOutlet = z.infer<typeof SpecAndOutlet>;

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
export class ZZWorkflowSpec<Specs> extends ZZJobSpec<WorkflowJobParams> {
  public readonly connections: CanonicalConnection[];
  private readonly defGraph: DefGraph;
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
        const instantiatedGraph = instantiateFromDefGraph({
          defGraph: this.defGraph,
          groupId,
        });

        const jobNodes = instantiatedGraph
          .nodes()
          .filter((n) => instantiatedGraph.getNodeAttributes(n).type === "job");

        for (let i = 0; i < jobNodes.length; i++) {
          const jobNode = instantiatedGraph.getNodeAttributes(
            jobNodes[i]
          ) as JobNode;
          const childSpecName = jobNode.specName;
          const childJobSpec = ZZJobSpec.lookupByName(childSpecName);
          const jDef = {
            jobId: jobNode.jobId,
            childrenJobParams,
          };
          await childJobSpec.requestJob(jDef);
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

    const instantiatedGraph = instantiateFromDefGraph({
      defGraph: this.defGraph,
      groupId: jobGroupId,
    });
    await this.requestJob({
      jobId: jobGroupId,
      jobParams: {
        groupId: jobGroupId,
        jobParams: childJobParams,
      },
    });

    // Create interfaces for inputs and outputs

    const identifySpecAndJobIdBySpecQuery = (
      specQuery: UniqueSpecQuery
    ): { spec: ZZJobSpec<any, any, any>; jobId: string } => {
      const specInfo = convertUniqueSpec(specQuery);
      const jobNodeId = instantiatedGraph.findNode((id, n) => {
        return (
          n.type === "job" &&
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
      const jobId = (instantiatedGraph.getNodeAttributes(jobNodeId) as JobNode)
        .jobId;
      const childSpec = ZZJobSpec.lookupByName(specInfo.specName);

      return { spec: childSpec, jobId };
    };

    const jobIdBySpec = (specQuery: UniqueSpecQuery) => {
      const { jobId } = identifySpecAndJobIdBySpecQuery(specQuery);
      return jobId;
    };

    const inputs = {
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveInputsForJob(jobId);
      },
    };

    const outputs = {
      bySpec: (specQuery: UniqueSpecQuery) => {
        const { spec: childSpec, jobId } =
          identifySpecAndJobIdBySpecQuery(specQuery);
        return childSpec._deriveOutputsForJob(jobId);
      },
    };

    // console.log("countByName", countByName);
    return new ZZWorkflow({
      jobIdBySpec,
      inputs,
      outputs,
      jobGroupDef: this,
    });
  }
}

export class ZZWorkflow<Specs> {
  public readonly jobIdBySpec: (specQuery: UniqueSpecQuery) => string;
  public readonly inputs: {
    bySpec: <P, I, O>(
      spec: ZZJobSpec<P, I, O>
    ) => ReturnType<ZZJobSpec<P, I, O>["_deriveInputsForJob"]>;
  };
  public readonly outputs: {
    bySpec: <P, I, O>(
      spec: ZZJobSpec<P, I, O>
    ) => ReturnType<ZZJobSpec<P, I, O>["_deriveOutputsForJob"]>;
  };
  public readonly jobGroupDef: ZZWorkflowSpec<Specs>;
  constructor({
    jobIdBySpec,
    inputs,
    outputs,
    jobGroupDef,
  }: {
    jobGroupDef: ZZWorkflowSpec<Specs>;
    jobIdBySpec: (specQuery: UniqueSpecQuery) => string;
    inputs: {
      bySpec: <P, I, O>(
        spec: ZZJobSpec<P, I, O>
      ) => ReturnType<ZZJobSpec<P, I, O>["_deriveInputsForJob"]>;
    };
    outputs: {
      bySpec: <P, I, O>(
        spec: ZZJobSpec<P, I, O>
      ) => ReturnType<ZZJobSpec<P, I, O>["_deriveOutputsForJob"]>;
    };
  }) {
    this.jobIdBySpec = jobIdBySpec;
    this.inputs = inputs;
    this.outputs = outputs;
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
function convertSpecOrName(specOrName: SpecOrName): string {
  if (typeof specOrName === "string") {
    return specOrName;
  } else {
    return specOrName.name;
  }
}

function convertUniqueSpec(uniqueSpec: UniqueSpecQuery): {
  specName: string;
  uniqueLabel?: string;
} {
  if ("spec" in (uniqueSpec as any) && "label" in (uniqueSpec as any)) {
    return {
      specName: convertSpecOrName(
        (
          uniqueSpec as {
            spec: SpecOrName;
            label: string;
          }
        ).spec
      ),
      uniqueLabel: (
        uniqueSpec as {
          spec: SpecOrName;
          label: string;
        }
      ).label,
    };
  } else {
    return {
      specName: convertSpecOrName(uniqueSpec as any),
    };
  }
}

function convertSpecAndOutlet(specAndOutlet: SpecAndOutlet): {
  specName: string;
  uniqueLabel?: string;
  key: string;
} {
  if (Array.isArray(specAndOutlet)) {
    const [uniqueSpec, key] = specAndOutlet;
    const uniqueLabel = convertUniqueSpec(uniqueSpec).uniqueLabel;
    return {
      specName: convertUniqueSpec(uniqueSpec).specName,
      ...(uniqueLabel ? { uniqueLabel } : {}),
      key: key,
    };
  } else {
    const converted = convertUniqueSpec(specAndOutlet);
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

import Graph from "graphology";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { v4 } from "uuid";
import { ZZEnv } from "../jobs/ZZEnv";

type SpecNode = {
  type: "spec";
  specName: string;
  uniqueLabel?: string;
};
type OutletNode = {
  type: "outlet";
  key: string;
};
type InletNode = {
  type: "inlet";
  key: string;
};
type DefGraphNode =
  | SpecNode
  | {
      type: "stream";
    }
  | InletNode
  | OutletNode;

type InferNodeData<T extends DefGraphNode["type"]> = Extract<
  DefGraphNode,
  { type: T }
>;

type DefNodeType = DefGraphNode["type"];
type DefGraph = Graph<DefGraphNode>;

type JobNode = {
  type: "job";
  jobId: string;
  specName: string;
  uniqueLabel?: string;
};

type InstantiatedGraph = Graph<
  | JobNode
  | {
      type: "stream";
      streamId: string;
    }
  | {
      type: "inlet";
      key: string;
    }
  | {
      type: "outlet";
      key: string;
    }
>;

function convertedConnectionsToGraph(
  convertedConnections: CanonicalConnection[]
): DefGraph {
  const graph: DefGraph = new Graph();

  function createOrGetNodeId<T extends DefNodeType>(
    id: string,
    data: InferNodeData<T>
  ): string {
    const nodeId = `${data.type}_${id}`;
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
      type: "spec",
    });
    const fromOutletNodeId = createOrGetNodeId(
      `${fromSpecIdentifier}/${from.key}`,
      { type: "outlet", key: from.key }
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
      type: "stream",
    });
    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const toInletNodeId = createOrGetNodeId(`${toSpecIdentifier}/${to.key}`, {
      type: "inlet",
      key: to.key,
    });
    const toUniqueLabel = to.uniqueLabel;
    const toSpecNodeId = createOrGetNodeId(toSpecIdentifier, {
      specName: to.specName,
      ...(toUniqueLabel ? { uniqueLabel: toUniqueLabel } : {}),
      type: "spec",
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
    if (node.type === "spec") {
      const jobId = `[${groupId}]${uniqueSpecIdentifier(node)}`;
      g.addNode(jobId, {
        ...node,
        type: "job",
        jobId,
      });
      jobNodeIdBySpecNodeId[nodeId] = jobId;
    } else if (node.type === "stream") {
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
        type: "stream",
        streamId,
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
      fromNode.type === "spec"
        ? jobNodeIdBySpecNodeId[from]
        : fromNode.type === "stream"
        ? streamNodeIdByStreamId[from]
        : from;
    const newTo =
      toNode.type === "spec"
        ? jobNodeIdBySpecNodeId[to]
        : toNode.type === "stream"
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
