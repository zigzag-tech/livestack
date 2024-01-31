import Graph from "graphology";
import { IOSpec } from "@livestack/shared";

export type SpecNode = {
  nodeType: "spec";
  specName: string;
  uniqueSpecLabel?: string;
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
  nodeType: "stream";
};

export type DefGraphNode = { label: string } & (
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

export type JobNode = {
  nodeType: "job";
  jobId: string;
  specName: string;
  uniqueSpecLabel?: string;
};

export type InstantiatedGraph = Graph<
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

export class DefGraph extends Graph<DefGraphNode> {
  private streamNodeIdBySpecIdentifierTypeAndTag: {
    [k: `${string}::${string}/${string}`]: string;
  } = {};

  constructor() {
    super({ multi: false });
  }

  public addSingleSpec(spec: IOSpec<any, any, any, any>) {
    const specIdentifier = uniqueSpecIdentifier({
      spec,
    });

    const specNodeId = this.createOrGetNodeId(specIdentifier, {
      nodeType: "spec",
      specName: spec.name,
      label: specIdentifier,
    });

    // add inlet and outlet nodes, their edges, and the connected stream node and edges
    for (const tag of spec.inputDefSet.keys) {
      const tagStr = tag.toString();
      const inletIdentifier = `${specIdentifier}/${tagStr}`;
      const inletNodeId = this.createOrGetNodeId(inletIdentifier, {
        nodeType: "inlet",
        tag: tagStr,
        label: inletIdentifier,
      });

      this.ensureEdge(inletNodeId, specNodeId);

      const streamId =
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

      const streamNodeId = this.createOrGetNodeId(streamId, {
        nodeType: "stream",
        label: streamId,
      });

      this.ensureEdge(streamNodeId, inletNodeId);
    }

    for (const tag of spec.outputDefSet.keys) {
      const tagStr = tag.toString();
      const outletIdentifier = `${specIdentifier}/${tagStr}`;
      const outletNodeId = this.createOrGetNodeId(outletIdentifier, {
        nodeType: "outlet",
        tag: tagStr,
        label: outletIdentifier,
      });

      this.ensureEdge(specNodeId, outletNodeId);

      const streamId =
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

      const streamNodeId = this.createOrGetNodeId(streamId, {
        nodeType: "stream",
        label: streamId,
      });

      this.ensureEdge(outletNodeId, streamNodeId);
    }
  }

  public addConnectedDualSpecs(
    from: CanonicalConnectionPoint,
    to: CanonicalConnectionPoint
  ) {
    const fromSpecIdentifier = uniqueSpecIdentifier(from);

    const fromSpecNodeId = this.createOrGetNodeId(fromSpecIdentifier, {
      specName: from.spec.name,
      ...(from.uniqueSpecLabel
        ? { uniqueSpecLabel: from.uniqueSpecLabel }
        : {}),
      nodeType: "spec",
      label: fromSpecIdentifier,
    });
    const fromOutletNodeId = this.createOrGetNodeId(
      `${fromSpecIdentifier}/${from.tagInSpec}`,
      {
        nodeType: "outlet",
        tag: from.tagInSpec,
        label: `${fromSpecIdentifier}/${from.tagInSpec}`,
      }
    );

    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const id = `${toSpecIdentifier}/${to.tagInSpec}`;
    const toInletNodeId = this.createOrGetNodeId(id, {
      nodeType: "inlet",
      tag: to.tagInSpec,
      label: id,
    });
    const toUniqueLabel = to.uniqueSpecLabel;
    const toSpecNodeId = this.createOrGetNodeId(toSpecIdentifier, {
      specName: to.spec.name,
      ...(toUniqueLabel ? { uniqueSpecLabel: toUniqueLabel } : {}),
      nodeType: "spec",
      label: toSpecIdentifier,
    });

    const streamId =
      this.streamNodeIdBySpecIdentifierTypeAndTag[
        `${fromSpecIdentifier}::${from.tagInSpec}/${to.tagInSpec}`
      ] ||
      this.streamNodeIdBySpecIdentifierTypeAndTag[
        `${toSpecIdentifier}::${to.tagInSpec}/${from.tagInSpec}`
      ] ||
      uniqueStreamIdentifier({
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

    const streamNodeId = this.createOrGetNodeId(streamId, {
      nodeType: "stream",
      label: streamId,
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
  }

  private ensureEdge(from: string, to: string) {
    if (!super.hasEdge(from, to)) {
      super.addEdge(from, to);
    }
  }
  private createOrGetNodeId<T extends DefNodeType>(
    id: string,
    data: InferNodeData<T>
  ): string {
    const nodeId = `${data.nodeType}_${id}`;
    if (!this.hasNode(nodeId)) {
      this.addNode(nodeId, { ...data });
    }
    return nodeId;
  }
  // private addToSpecByNodeIdMap(ss: CanonicalSpecAndOutlet) {
  //   const identifier = uniqueSpecIdentifier(ss);
  //   this.specByNodeId.set(identifier, ss.spec);
  // }

  public instantiate({ groupId }: { groupId: string }): InstantiatedGraph {
    const g: InstantiatedGraph = new Graph();

    const nodes = this.nodes();
    const jobNodeIdBySpecNodeId: { [k: string]: string } = {};
    const streamNodeIdByStreamId: { [k: string]: string } = {};

    for (const nodeId of nodes) {
      const node = this.getNodeAttributes(nodeId);
      if (node.nodeType === "spec") {
        const jobId = `[${groupId}]${uniqueSpecIdentifier(node)}`;
        g.addNode(jobId, {
          ...node,
          nodeType: "job",
          jobId,
        });
        jobNodeIdBySpecNodeId[nodeId] = jobId;
      } else if (node.nodeType === "stream") {
        const { source, targets } = this.getNodesConnectedToStream(nodeId);
        const streamId = deriveStreamId({
          groupId,
          ...(source
            ? {
                from: {
                  specName: source.specNode.specName,
                  uniqueSpecLabel: source.specNode.uniqueSpecLabel,
                  tag: source.outletNode.tag,
                },
              }
            : {}),

          // TODO: handle multiple targets
          ...(targets.length > 0
            ? {
                to: {
                  specName: targets[0].specNode.specName,
                  uniqueSpecLabel: targets[0].specNode.uniqueSpecLabel,
                  tag: targets[0].inletNode.tag,
                },
              }
            : {}),
        });
        if (!g.hasNode(streamId)) {
          g.addNode(streamId, {
            nodeType: "stream",
            streamId,
            label: streamId,
          });
        }
        streamNodeIdByStreamId[nodeId] = streamId;
      } else {
        if (!g.hasNode(nodeId)) {
          g.addNode(nodeId, node);
        }
      }
    }

    const edges = this.edges();

    for (const edgeId of edges) {
      const from = this.source(edgeId);
      const to = this.target(edgeId);
      const fromNode = this.getNodeAttributes(from);
      const toNode = this.getNodeAttributes(to);

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
      if (!g.hasEdge(newFrom, newTo)) {
        g.addEdge(newFrom, newTo);
      }
    }

    return g;
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

  public getNodesConnectedToStream(streamNodeId: string) {
    // one source, multiple targets
    let source: {
      specNode: SpecNode;
      outletNode: OutletNode;
    } | null = null;

    const [ie] = this.inboundEdges(streamNodeId) as (string | undefined)[];
    if (!ie) {
      source = null;
    } else {
      const outletNodeId = this.source(ie);
      const outletNode = this.getNodeAttributes(outletNodeId) as OutletNode;
      const [ie2] = this.inboundEdges(outletNodeId);
      const sourceSpecNodeId = this.source(ie2);
      const sourceSpecNode = this.getNodeAttributes(
        sourceSpecNodeId
      ) as SpecNode;
      source = {
        specNode: sourceSpecNode,
        outletNode,
      };
    }

    let targets: {
      specNode: SpecNode;
      inletNode: InletNode;
    }[] = [];
    const oes = this.outboundEdges(streamNodeId) as (string | undefined)[];

    oes.forEach((oe) => {
      if (oe) {
        const inletNodeId = this.target(oe);
        const inletNode = this.getNodeAttributes(inletNodeId) as InletNode;
        const [oe2] = this.outboundEdges(inletNodeId);
        const targetSpecNodeId = this.target(oe2);
        const targetSpecNode = this.getNodeAttributes(
          targetSpecNodeId
        ) as SpecNode;
        targets.push({
          specNode: targetSpecNode,
          inletNode,
        });
      }
    });

    return {
      source,
      targets,
    };
  }
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
          ? `[${from.uniqueSpecLabel}]`
          : ""
      }/${from.tag}`
    : "(*)";
  const toStr = !!to
    ? `${to.specName}${
        to.uniqueSpecLabel && to.uniqueSpecLabel !== "default_label"
          ? `[${to.uniqueSpecLabel}]`
          : ""
      }/${to.tag}`
    : "(*)";
  return `${fromStr}>>${toStr}`;
}

export function deriveStreamId({
  groupId,
  from,
  to,
}: { groupId: string } & Parameters<typeof uniqueStreamIdentifier>[0]) {
  const suffix = uniqueStreamIdentifier({ from, to });
  return `stream-${groupId}::${suffix}`;
}
