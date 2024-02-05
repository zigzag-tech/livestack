import Graph from "graphology";
import { InletNode, OutletNode, AliasNode, DefGraph } from "./DefGraph";
import { IOSpec } from "@livestack/shared";
import { Attributes } from "graphology-types";
import { SpecNode, RootSpecNode } from "./DefGraph";

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
    | AliasNode
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
export function uniqueSpecIdentifier({
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
