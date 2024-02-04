import Graph from "graphology";
import { IOSpec } from "@livestack/shared";
import { Attributes } from "graphology-types";

export type SpecNode = {
  nodeType: "spec";
  specName: string;
  uniqueSpecLabel?: string;
};
export type RootSpecNode = {
  nodeType: "root-spec";
  specName: string;
};
export type OutletNode = {
  nodeType: "outlet";
  tag: string;
};
export type InletNode = {
  nodeType: "inlet";
  tag: string;
};
export type StreamDefNode = {
  nodeType: "stream-def";
  streamDefId: string;
};

export type DefGraphNode = { label: string } & (
  | RootSpecNode
  | SpecNode
  | StreamDefNode
  | InletNode
  | OutletNode
);
export type InferNodeData<T extends DefGraphNode["nodeType"]> = Extract<
  DefGraphNode,
  { nodeType: T }
>;
export type DefNodeType = DefGraphNode["nodeType"];
export type JobId = `[${string}]${string}`;
export type RootJobNode = {
  nodeType: "root-job";
  jobId: string;
  specName: string;
};
export type JobNode = {
  nodeType: "job";
  jobId: JobId;
  specName: string;
  uniqueSpecLabel?: string;
};

export type StreamNode = {
  nodeType: "stream";
  streamId: string;
};

export class InstantiatedGraph extends Graph<
  { label: string } & (
    | RootJobNode
    | JobNode
    | StreamNode
    | InletNode
    | OutletNode
  )
> {
  constructor({
    contextId,
    defGraph,
    streamIdOverrides,
    rootJobId,
  }: {
    defGraph: DefGraph;
    contextId: string;
    rootJobId: string;
    streamIdOverrides: StreamIdOverridesForRootSpec;
  }) {
    super({ multi: false });
    this.instantiate({ defGraph, contextId, rootJobId, streamIdOverrides });
  }

  private instantiate({
    defGraph,
    contextId,
    streamIdOverrides,
    rootJobId,
  }: {
    defGraph: DefGraph;
    contextId: string;
    streamIdOverrides: StreamIdOverridesForRootSpec;
    rootJobId: string;
  }) {
    const nodes = defGraph.nodes();
    const childJobNodeIdBySpecNodeId: { [k: string]: string } = {};
    const streamNodeIdByStreamId: { [k: string]: string } = {};

    for (const nodeId of nodes) {
      const node = defGraph.getNodeAttributes(nodeId);
      if (node.nodeType === "root-spec") {
        this.addNode(rootJobId, {
          ...node,
          nodeType: "root-job",
          jobId: rootJobId,
        });
      } else if (node.nodeType === "spec") {
        const jobId: JobId = `[${contextId}]${uniqueSpecIdentifier(node)}`;
        this.addNode(jobId, {
          ...node,
          nodeType: "job",
          jobId,
        });
        childJobNodeIdBySpecNodeId[nodeId] = jobId;
      } else if (node.nodeType === "stream-def") {
        let streamId: string | null = null;
        const { source, targets } = getNodesConnectedToStream(defGraph, nodeId);
        if (source?.origin.nodeType === "root-spec") {
          const tag = source.outletNode.tag;
          const streamIdOverride = streamIdOverrides[`out/${tag}`];
          if (streamIdOverride) {
            streamId = streamIdOverride;
          }
        } else {
          for (const target of targets) {
            const tag = target.inletNode.tag;
            const streamIdOverride = streamIdOverrides[`in/${tag}`];
            if (streamIdOverride) {
              streamId = streamIdOverride;
              break;
            }
          }
        }

        if (!streamId) {
          streamId = `[${contextId}]${node.streamDefId}`;
        }

        if (!this.hasNode(streamId)) {
          this.addNode(streamId, {
            nodeType: "stream",
            streamId,
            label: streamId,
          });
        }
        streamNodeIdByStreamId[nodeId] = streamId;
      } else {
        if (!this.hasNode(nodeId)) {
          this.addNode(nodeId, node);
        }
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
          ? childJobNodeIdBySpecNodeId[from]
          : fromNode.nodeType === "root-spec"
          ? rootJobId
          : fromNode.nodeType === "stream-def"
          ? streamNodeIdByStreamId[from]
          : from;
      const newTo =
        toNode.nodeType === "spec"
          ? childJobNodeIdBySpecNodeId[to]
          : toNode.nodeType === "root-spec"
          ? rootJobId
          : toNode.nodeType === "stream-def"
          ? streamNodeIdByStreamId[to]
          : to;
      if (!this.hasEdge(newFrom, newTo)) {
        try {
          this.addEdge(newFrom, newTo);
        } catch (e) {
          throw e;
        }
      }
    }
  }
}

export type StreamIdOverridesForRootSpec = {
  [k: `${"in" | "out"}/${string}`]: string;
};

export class DefGraph extends Graph<DefGraphNode> {
  private streamNodeIdBySpecIdentifierTypeAndTag: {
    [k: `${string}::${string}/${string}`]: string;
  } = {};

  constructor({ root }: { root: IOSpec<any, any, any, any> }) {
    super({ multi: false });
    this.addSingleRootSpec(root);
  }

  private addSingleRootSpec(spec: IOSpec<any, any, any, any>) {
    const specIdentifier = uniqueSpecIdentifier({
      spec,
    });

    const specNodeId = this.ensureNode(specIdentifier, {
      nodeType: "root-spec",
      specName: spec.name,
      label: specIdentifier,
    });

    // add inlet and outlet nodes, their edges, and the connected stream node and edges
    for (const tag of spec.inputDefSet.keys) {
      const tagStr = tag.toString();
      const inletIdentifier = `${specIdentifier}/${tagStr}`;
      const inletNodeId = this.ensureNode(inletIdentifier, {
        nodeType: "inlet",
        tag: tagStr,
        label: inletIdentifier,
      });

      this.ensureEdge(inletNodeId, specNodeId);

      const streamDefId =
        this.streamNodeIdBySpecIdentifierTypeAndTag[
          `${specIdentifier}::${tagStr}/${tagStr}`
        ] ||
        uniqueStreamIdentifier({
          to: {
            specName: spec.name,
            uniqueSpecLabel: undefined,
            tag: tagStr,
          },
        });

      const streamNodeId = this.ensureNode(streamDefId, {
        nodeType: "stream-def",
        label: streamDefId,
        streamDefId,
      });

      this.ensureEdge(streamNodeId, inletNodeId);
    }

    for (const tag of spec.outputDefSet.keys) {
      const tagStr = tag.toString();
      const outletIdentifier = `${specIdentifier}/${tagStr}`;
      const outletNodeId = this.ensureNode(outletIdentifier, {
        nodeType: "outlet",
        tag: tagStr,
        label: outletIdentifier,
      });

      this.ensureEdge(specNodeId, outletNodeId);

      const streamDefId =
        this.streamNodeIdBySpecIdentifierTypeAndTag[
          `${specIdentifier}::${tagStr}/${tagStr}`
        ] ||
        uniqueStreamIdentifier({
          from: {
            specName: spec.name,
            uniqueSpecLabel: undefined,
            tag: tagStr,
          },
        });

      const streamNodeId = this.ensureNode(streamDefId, {
        nodeType: "stream-def",
        label: streamDefId,
        streamDefId,
      });

      this.ensureEdge(outletNodeId, streamNodeId);
    }
  }

  public addConnectedDualSpecs(
    from: CanonicalConnectionPoint,
    to: CanonicalConnectionPoint
  ) {
    const fromSpecIdentifier = uniqueSpecIdentifier(from);

    const fromSpecNodeId = this.ensureNode(fromSpecIdentifier, {
      specName: from.spec.name,
      ...(from.uniqueSpecLabel
        ? { uniqueSpecLabel: from.uniqueSpecLabel }
        : {}),
      nodeType: "spec",
      label: fromSpecIdentifier,
    });
    const fromOutletNodeId = this.ensureNode(
      `${fromSpecIdentifier}/${from.tagInSpec}`,
      {
        nodeType: "outlet",
        tag: from.tagInSpec,
        label: `${fromSpecIdentifier}/${from.tagInSpec}`,
      }
    );

    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const id = `${toSpecIdentifier}/${to.tagInSpec}`;
    const toInletNodeId = this.ensureNode(id, {
      nodeType: "inlet",
      tag: to.tagInSpec,
      label: id,
    });
    const toUniqueLabel = to.uniqueSpecLabel;
    const toSpecNodeId = this.ensureNode(toSpecIdentifier, {
      specName: to.spec.name,
      ...(toUniqueLabel ? { uniqueSpecLabel: toUniqueLabel } : {}),
      nodeType: "spec",
      label: toSpecIdentifier,
    });

    const streamDefId = uniqueStreamIdentifier({
      from: {
        specName: from.spec.name,
        uniqueSpecLabel: from.uniqueSpecLabel,
        tag: from.tagInSpec,
      },
      to: {
        specName: to.spec.name,
        uniqueSpecLabel: to.uniqueSpecLabel,
        tag: to.tagInSpec,
      },
    });

    const streamNodeId = this.ensureNode(streamDefId, {
      nodeType: "stream-def",
      label: streamDefId,
      streamDefId,
    });

    this.ensureEdge(fromSpecNodeId, fromOutletNodeId);
    this.ensureEdge(fromOutletNodeId, streamNodeId);
    this.ensureEdge(streamNodeId, toInletNodeId);
    this.ensureEdge(toInletNodeId, toSpecNodeId);

    this.streamNodeIdBySpecIdentifierTypeAndTag[
      `${fromSpecIdentifier}::${from.tagInSpec}/${to.tagInSpec}`
    ] = streamNodeId;
    this.streamNodeIdBySpecIdentifierTypeAndTag[
      `${toSpecIdentifier}::${to.tagInSpec}/${from.tagInSpec}`
    ] = streamNodeId;

    return {
      fromSpecNodeId,
      toSpecNodeId,
      streamNodeId,
      fromOutletNodeId,
      toInletNodeId,
    };
  }

  public ensureParentChildRelation(
    parentSpecNodeId: string,
    childSpecNodeId: string
  ) {
    this.ensureEdge(childSpecNodeId, parentSpecNodeId, {
      type: "parent-child",
    });
  }

  private ensureEdge(from: string, to: string, attributes: Attributes = {}) {
    if (!super.hasEdge(from, to)) {
      super.addEdge(from, to, attributes);
    } else {
      const edge = this.getEdgeAttributes(from, to);
      for (const [k, v] of Object.entries(attributes)) {
        if (edge[k] !== v) {
          edge[k] = v;
        }
      }
    }
  }

  private ensureNode<T extends DefNodeType>(
    id: string,
    data: InferNodeData<T>
  ): string {
    const nodeId = `${data.nodeType}_${id}`;
    if (!this.hasNode(nodeId)) {
      this.addNode(nodeId, { ...data });
    }
    return nodeId;
  }

  public getRootSpecNodeId(): string {
    const nodes = this.filterNodes(
      (_, attrs) => attrs.nodeType === "root-spec"
    );
    if (nodes.length !== 1) {
      throw new Error(
        `Expected exactly one root spec node, got ${nodes.length}.`
      );
    }
    return nodes[0];
  }

  public getSpecNodeIds() {
    return this.filterNodes((_, attrs) => attrs.nodeType === "spec");
  }

  public getInboundNodeSets(specNodeId: string) {
    const inletNodeIds = this.filterInboundNeighbors(
      specNodeId,
      (n, attrs) => attrs.nodeType === "inlet"
    );

    return inletNodeIds.map((inletNodeId) => {
      const [ie] = this.inboundEdges(inletNodeId) as (string | undefined)[];
      const streamNodeId = this.source(ie);
      return {
        inletNode: {
          ...(this.getNodeAttributes(inletNodeId) as InletNode),
          id: inletNodeId,
        },
        streamNode: {
          ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
          id: streamNodeId,
        },
      };
    });
  }

  public getOutboundNodeSets(specNodeId: string) {
    const outletNodeIds = this.filterOutboundNeighbors(
      specNodeId,
      (n, attrs) => attrs.nodeType === "outlet"
    );

    return outletNodeIds.map((outletNodeId) => {
      const [oe] = this.outboundEdges(outletNodeId) as (string | undefined)[];
      const streamNodeId = this.target(oe);
      return {
        outletNode: {
          ...(this.getNodeAttributes(outletNodeId) as OutletNode),
          id: outletNodeId,
        },
        streamNode: {
          ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
          id: streamNodeId,
        },
      };
    });
  }
}

export function getNodesConnectedToStream<
  G extends DefGraph | InstantiatedGraph
>(g: G, streamNodeId: string) {
  // one source, multiple targets
  type NT = G extends DefGraph ? SpecNode | RootSpecNode : JobNode;
  let source: {
    origin: NT;
    outletNode: OutletNode;
  } | null = null;
  const [outletNodeId] = g.inboundNeighbors(streamNodeId);
  if (!outletNodeId) {
    source = null;
  } else {
    const outletNode = g.getNodeAttributes(outletNodeId) as OutletNode;
    const [sourceSpecNodeId] = g.inboundNeighbors(outletNodeId);
    const sourceSpecNode = g.getNodeAttributes(
      sourceSpecNodeId
    ) as Attributes as NT;
    source = {
      origin: sourceSpecNode,
      outletNode,
    };
  }

  let targets: {
    inletNode: InletNode;
    destination: G extends DefGraph ? SpecNode | RootSpecNode : JobNode;
  }[] = [];
  const inletNodeIds = g.outboundNeighbors(streamNodeId) as (
    | string
    | undefined
  )[];

  inletNodeIds.forEach((inletNodeId) => {
    if (inletNodeId) {
      const inletNode = g.getNodeAttributes(inletNodeId) as InletNode;
      const [targetSpecNodeId] = g.outboundNeighbors(inletNodeId);
      const targetSpecNode = g.getNodeAttributes(
        targetSpecNodeId
      ) as Attributes as NT;
      targets.push({
        destination: targetSpecNode,
        inletNode,
      });
    }
  });

  return {
    source,
    targets,
  };
}

export type CanonicalConnectionPoint = {
  spec: IOSpec<any, any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec: string;
  tagInSpecType: "input" | "output";
};

export type CanonicalConnection = [
  CanonicalConnectionPoint,
  CanonicalConnectionPoint
];
function uniqueSpecIdentifier({
  specName,
  spec,
  uniqueSpecLabel,
}: {
  specName?: string;
  spec?: IOSpec<any, any, any, any>;
  uniqueSpecLabel?: string;
}) {
  specName = specName ?? spec?.name;
  if (!specName) {
    throw new Error("specName or spec must be provided");
  }
  return `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`;
}
export function uniqueStreamIdentifier({
  from,
  to,
}: {
  from?: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
  };
  to?: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
  };
}) {
  const fromStr = !!from
    ? `${from.specName}${
        from.uniqueSpecLabel && from.uniqueSpecLabel !== "default_label"
          ? `(${from.uniqueSpecLabel})`
          : ""
      }/${from.tag}`
    : "(*)";
  const toStr = !!to
    ? `${to.specName}${
        to.uniqueSpecLabel && to.uniqueSpecLabel !== "default_label"
          ? `(${to.uniqueSpecLabel})`
          : ""
      }/${to.tag}`
    : "(*)";
  return `${fromStr}>>${toStr}`;
}
