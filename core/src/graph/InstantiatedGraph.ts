import Graph from "graphology";
import { InletNode, OutletNode, AliasNode } from "./DefGraph";
import { Attributes } from "graphology-types";
import { SpecNode, RootSpecNode } from "./DefGraph";
import {
  DefGraph,
  NodeType,
  loadDefGraphFromJson,
} from "livestack-shared-crosslang-js";

import { genSpecIdentifier } from "livestack-shared-wasm";


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

    const nodeIds = defGraph.nodes();
    const childJobNodeIdBySpecNodeId: { [k: string]: string } = {};
    const streamNodeIdByStreamId: { [k: string]: string } = {};

    for (const nodeId of nodeIds) {
      const node = defGraph.getNodeAttributes(nodeId);
      if (node.nodeType === NodeType.RootSpec) {
        this.addNode(rootJobId, {
          ...(node as RootSpecNode & {
            label: string;
          }),
          nodeType: "root-job",
          jobId: rootJobId,
        });
      } else if (node.nodeType === NodeType.Spec) {
        const jobId: JobId = `[${contextId}]${genSpecIdentifier(
          node.specName!,
          node.uniqueSpecLabel
        )}`;
        this.addNode(jobId, {
          ...(node as SpecNode & {
            label: string;
          }),
          nodeType: "job",
          jobId,
        });
        childJobNodeIdBySpecNodeId[nodeId] = jobId;
      } else if (node.nodeType === NodeType.StreamDef) {
        let streamId: string | null = null;
        const { source, targets } = getNodesConnectedToStream(defGraph, nodeId);
        if (source?.origin.nodeType === NodeType.RootSpec) {
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
      } else if (node.nodeType === NodeType.Inlet) {
        this.addNode(nodeId, {
          ...(node as InletNode & {
            label: string;
          }),
          hasTransform: inletHasTransformOverridesByTag[node.tag!],
        });
      } else {
        if (!this.hasNode(nodeId)) {
          this.addNode(nodeId, node as OutletNode & { label: string });
        }
      }
    }

    const edges = defGraph.edges();

    for (const { source: from, target: to } of edges) {
      const fromNode = defGraph.getNodeAttributes(from);
      const toNode = defGraph.getNodeAttributes(to);

      const newFrom =
        fromNode.nodeType === NodeType.Spec
          ? childJobNodeIdBySpecNodeId[from]
          : fromNode.nodeType === NodeType.RootSpec
          ? rootJobId
          : fromNode.nodeType === NodeType.StreamDef
          ? streamNodeIdByStreamId[from]
          : from;
      const newTo =
        toNode.nodeType === NodeType.Spec
          ? childJobNodeIdBySpecNodeId[to]
          : toNode.nodeType === NodeType.RootSpec
          ? rootJobId
          : toNode.nodeType === NodeType.StreamDef
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
      defGraph: this.defGraph.toJson(),
    };
    return newJ;
  }

  static loadFromJSON(json: ReturnType<InstantiatedGraph["toJSON"]>) {
    return new InstantiatedGraph({
      contextId: json.contextId,
      defGraph: loadDefGraphFromJson(json.defGraph),
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
>(g: G, streamNodeId: number | string) {
  // one source, multiple targets
  type NT = G extends DefGraph ? SpecNode | RootSpecNode : JobNode;

  let targets: {
    inletNode: InletNode;
    destination: G extends DefGraph ? SpecNode | RootSpecNode : JobNode;
  }[] = [];
  const inletNodeIds = g
    .outboundNeighbors(streamNodeId as number)
    .filter(
      (nId) => g.getNodeAttributes(nId as number).nodeType === NodeType.Inlet
    ) as (string | number | undefined)[];

  inletNodeIds.forEach((inletNodeId) => {
    if (inletNodeId) {
      const inletNode = g.getNodeAttributes(inletNodeId as number) as InletNode;
      const [targetSpecNodeId] = g
        .outboundNeighbors(inletNodeId as number)
        .filter(
          (nId) =>
            g.getNodeAttributes(nId as number).nodeType === NodeType.Spec ||
            g.getNodeAttributes(nId as number).nodeType === NodeType.RootSpec ||
            g.getNodeAttributes(nId as number).nodeType === "job" ||
            g.getNodeAttributes(nId as number).nodeType === "root-job"
        ) as (string | number | undefined)[];
      const targetSpecNode = g.getNodeAttributes(
        targetSpecNodeId as number
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
>(g: G, streamNodeId: number | string) {
  // one source, multiple targets
  type NT = G extends DefGraph ? SpecNode | RootSpecNode : JobNode;
  let source: {
    origin: NT;
    outletNode: OutletNode;
  } | null = null;
  const [outletNodeId] = g
    .inboundNeighbors(streamNodeId as number)
    .filter(
      (nId) => g.getNodeAttributes(nId as number).nodeType === NodeType.Outlet
    );
  if (!outletNodeId) {
    source = null;
  } else {
    const outletNode = g.getNodeAttributes(
      outletNodeId as number
    ) as OutletNode;

    const [sourceSpecNodeId] = g
      .inboundNeighbors(outletNodeId as number)
      .filter(
        (nId) =>
          g.getNodeAttributes(nId as number).nodeType === NodeType.Spec ||
          g.getNodeAttributes(nId as number).nodeType === NodeType.RootSpec ||
          g.getNodeAttributes(nId as number).nodeType === "job" ||
          g.getNodeAttributes(nId as number).nodeType === "root-job"
      );

    const sourceSpecNode = g.getNodeAttributes(
      sourceSpecNodeId as number
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
>(g: G, streamNodeId: number | string) {
  // one source, multiple targets
  const source = getSourceSpecNodeConnectedToStream(g, streamNodeId);
  const targets = getTargetSpecNodesConnectedToStream(g, streamNodeId);

  return {
    source,
    targets,
  };
}
