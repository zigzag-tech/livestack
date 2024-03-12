import Graph from "graphology";
import { InletNode, OutletNode, AliasNode, DefGraph } from "./DefGraph";
import { Attributes } from "graphology-types";
import { SpecNode, RootSpecNode } from "./DefGraph";
import { uniqueSpecIdentifier } from "livestack-shared-crosslang-js";

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
  public contextId: string;
  public rootJobId: string;
  public streamIdOverrides: StreamIdOverridesForRootSpec;
  public inletHasTransformOverridesByTag: Record<string, boolean>;
  public streamSourceSpecTypeByStreamId: Record<
    string,
    {
      specName: string;
      tag: string;
    }
  >;
  public defGraph: DefGraph;

  constructor({
    contextId,
    defGraph,
    streamIdOverrides,
    rootJobId,
    inletHasTransformOverridesByTag,
    streamSourceSpecTypeByStreamId,
  }: {
    defGraph: DefGraph;
    contextId: string;
    rootJobId: string;
    streamIdOverrides: StreamIdOverridesForRootSpec;
    inletHasTransformOverridesByTag: Record<string, boolean>;
    streamSourceSpecTypeByStreamId: Record<
      string,
      {
        specName: string;
        tag: string;
      }
    >;
  }) {
    super({ multi: false });
    this.contextId = contextId;
    this.rootJobId = rootJobId;
    this.streamIdOverrides = streamIdOverrides;
    this.inletHasTransformOverridesByTag = inletHasTransformOverridesByTag;
    this.streamSourceSpecTypeByStreamId = streamSourceSpecTypeByStreamId;
    this.defGraph = defGraph;

    this.instantiate();
  }

  private instantiate() {
    const contextId = this.contextId;
    const rootJobId = this.rootJobId;
    const streamIdOverrides = this.streamIdOverrides;
    const inletHasTransformOverridesByTag =
      this.inletHasTransformOverridesByTag;
    const defGraph = this.defGraph;

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
        const jobId: JobId = `[${contextId}]${uniqueSpecIdentifier(
          node.specName,
          node.uniqueSpecLabel
        )}`;
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
      } else if (node.nodeType === "inlet") {
        this.addNode(nodeId, {
          ...node,
          hasTransform: inletHasTransformOverridesByTag[node.tag],
        });
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

  public findStreamNodeIdConnectedToJob({
    jobId,
    type,
    tag,
  }: {
    jobId: string;
    type: "in" | "out";
    tag: string;
  }) {
    const streamNodeId = this.findNode((nId) => {
      const n = this.getNodeAttributes(nId);
      if (n.nodeType !== "stream") {
        return false;
      } else {
        const { source, targets } = getNodesConnectedToStream(this, nId);

        if (type === "out") {
          return (
            (source &&
              source.origin.jobId === jobId &&
              source.outletNode.tag === tag) ||
            false
          );
        } else if (type === "in") {
          return targets.some(
            (t) => t.destination.jobId === jobId && t.inletNode.tag === tag
          );
        }
      }
    });
    return streamNodeId;
  }

  public override toJSON() {
    const json = super.toJSON();
    const newJ = {
      ...json,
      contextId: this.contextId,
      rootJobId: this.rootJobId,
      streamIdOverrides: this.streamIdOverrides,
      inletHasTransformOverridesByTag: this.inletHasTransformOverridesByTag,
      streamSourceSpecTypeByStreamId: this.streamSourceSpecTypeByStreamId,
      defGraph: this.defGraph.toJSON(),
    };
    return newJ;
  }

  static loadFromJSON(json: ReturnType<InstantiatedGraph["toJSON"]>) {
    return new InstantiatedGraph({
      contextId: json.contextId,
      defGraph: DefGraph.loadFromJSON(json.defGraph),
      rootJobId: json.rootJobId,
      streamIdOverrides: json.streamIdOverrides,
      inletHasTransformOverridesByTag: json.inletHasTransformOverridesByTag,
      streamSourceSpecTypeByStreamId: json.streamSourceSpecTypeByStreamId,
    });
  }
}

export type StreamIdOverridesForRootSpec = {
  [k: `${"in" | "out"}/${string}`]: string;
};

export function getTargetSpecNodesConnectedToStream<
  G extends DefGraph | InstantiatedGraph
>(g: G, streamNodeId: string) {
  // one source, multiple targets
  type NT = G extends DefGraph ? SpecNode | RootSpecNode : JobNode;

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

  return targets;
}

export function getSourceSpecNodeConnectedToStream<
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

  return source;
}

export function getNodesConnectedToStream<
  G extends DefGraph | InstantiatedGraph
>(g: G, streamNodeId: string) {
  // one source, multiple targets
  const source = getSourceSpecNodeConnectedToStream(g, streamNodeId);
  const targets = getTargetSpecNodesConnectedToStream(g, streamNodeId);

  return {
    source,
    targets,
  };
}
