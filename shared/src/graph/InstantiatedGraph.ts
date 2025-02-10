import { DirectedGraph } from "graphology";
import {
  InletNode,
  OutletNode,
  AliasNode,
  loadDefGraphFromJson,
  SpecNode,
  RootSpecNode,
  genSpecIdentifier,
  ensureNodeJSOnly,
} from "./DefGraph";
import type { DefGraph, InstantiatedGraph } from "./wasm/livestack_shared_wasm";
export type { InstantiatedGraph };
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

export class InstantiatedGraphOld {
  // Internal graph wrapped privately
  private graph: DirectedGraph<{ label: string } & (RootJobNode | JobNode | StreamNode | InletNode | OutletNode | AliasNode)>;

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

  public readonly initPromise: Promise<void>;

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
    // Initialize the internal DirectedGraph with non-multi edges
    this.graph = new DirectedGraph({ multi: false });

    this.contextId = contextId;
    this.rootJobId = rootJobId;
    this.streamIdOverrides = streamIdOverrides;
    this.inletHasTransformOverridesByTag = inletHasTransformOverridesByTag;
    this.streamSourceSpecTypeByStreamId = streamSourceSpecTypeByStreamId;
    this.defGraph = defGraph;

    this.initPromise = this.instantiate();
  }

  private async instantiate() {
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
      if (node.nodeType === "root-spec") {
        this.graph.addNode(rootJobId, {
          ...(node as RootSpecNode & {
            label: string;
          }),
          nodeType: "root-job",
          jobId: rootJobId,
        });
      } else if (node.nodeType === "spec") {
        const jobId: JobId = `[${contextId}]${await genSpecIdentifier(
          node.specName!,
          node.uniqueSpecLabel || undefined
        )}`;
        this.graph.addNode(jobId, {
          ...(node as SpecNode & {
            label: string;
          }),
          nodeType: "job",
          jobId,
        });
        childJobNodeIdBySpecNodeId[nodeId] = jobId;
      } else if (node.nodeType === "stream-def") {
        let streamId: string | null = null;
        const { source, targets } = defGraph.getNodesConnectedToStream(nodeId);
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

        if (!this.graph.hasNode(streamId)) {
          this.graph.addNode(streamId, {
            nodeType: "stream",
            streamId,
            label: streamId,
          });
        }
        streamNodeIdByStreamId[nodeId] = streamId;
      } else if (node.nodeType === "inlet") {
        this.graph.addNode(nodeId, {
          ...(node as InletNode & {
            label: string;
          }),
          hasTransform: inletHasTransformOverridesByTag[node.tag!],
        });
      } else {
        if (!this.graph.hasNode(nodeId)) {
          this.graph.addNode(nodeId, node as OutletNode & { label: string });
        }
      }
    }

    const edges = defGraph.edges().results;

    for (const { source: from, target: to } of edges) {
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
      if (!this.graph.hasEdge(newFrom, newTo)) {
        try {
          this.graph.addEdge(newFrom, newTo);
        } catch (e) {
          throw e;
        }
      }
    }
  }

  /* Public wrapper methods delegating to the internal DirectedGraph */



  public nodes(): Array<string | number> {
    return this.graph.nodes();
  }

  public getNodeAttributes(nodeId: string | number) {
    return this.graph.getNodeAttributes(nodeId);
  }

  public findOutboundEdge(predicate: (edgeId: string | number) => boolean) {
    return this.graph.findOutboundEdge(predicate);
  }

  public findInboundEdge(predicate: (edgeId: string | number) => boolean) {
    return this.graph.findInboundEdge(predicate);
  }



  public inboundNeighbors(nodeId: string | number) {
    return this.graph.inboundNeighbors(nodeId);
  }

  public outboundNeighbors(nodeId: string | number) {
    return this.graph.outboundNeighbors(nodeId);
  }

  public edges() {
    return this.graph.edges();
  }

  public findNode(predicate: (nodeId: string | number) => boolean) {
    return this.nodes().find(predicate);
  }

  public inboundEdges(nodeId: string | number) {
    return this.graph.inboundEdges(nodeId);
  }

  public outboundEdges(nodeId: string | number) {
    return this.graph.outboundEdges(nodeId);
  }

  public source(edgeId: string | number) {
    return this.graph.source(edgeId);
  }

  public target(edgeId: string | number) {
    return this.graph.target(edgeId);
  }


  public filterNodes(predicate: (nodeId: string | number) => boolean) {
    return this.graph.filterNodes(predicate);
  }

  public filterEdges(predicate: (edgeId: string | number) => boolean) {
    return this.graph.filterEdges(predicate);
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
        const { source, targets } = this.getNodesConnectedToStream(nId);

        if (type === "out") {
          return (
            (source &&
              source.origin.nodeType === "root-job" &&
              source.outletNode.tag === tag) ||
            false
          );
        } else if (type === "in") {
          return targets.some(
            (t) => t.destination.jobId === jobId && t.inletNode.tag === tag
          );
        }
        return false;
      }
    });
    return streamNodeId;
  }

  public getTargetSpecNodesConnectedToStream(streamNodeId: number | string) {
    const targets: { inletNode: InletNode; destination: JobNode | RootJobNode }[] = [];
    const inletNodeIds = this.outboundNeighbors(Number(streamNodeId))
      .filter(nId => this.getNodeAttributes(Number(nId)).nodeType === "inlet");
    inletNodeIds.forEach(inletNodeId => {
      if (inletNodeId !== undefined) {
        const inletNode = this.getNodeAttributes(Number(inletNodeId)) as InletNode;
        const [targetSpecNodeId] = this.outboundNeighbors(Number(inletNodeId))
          .filter(nId => {
            const type = this.getNodeAttributes(Number(nId)).nodeType;
            return type === "job" || type === "root-job";
          });
        if (targetSpecNodeId !== undefined) {
          const targetSpecNode = this.getNodeAttributes(Number(targetSpecNodeId)) as (JobNode | RootJobNode);
          targets.push({ inletNode, destination: targetSpecNode });
        }
      }
    });
    return targets;
  }

  public getSourceSpecNodeConnectedToStream(streamNodeId: number | string) {
    const outletIds = this.inboundNeighbors(Number(streamNodeId))
      .filter(nId => this.getNodeAttributes(Number(nId)).nodeType === "outlet");
    const outletNodeId = outletIds[0];
    if (outletNodeId === undefined) return null;
    const outletNode = this.getNodeAttributes(Number(outletNodeId)) as OutletNode;
    const inboundIds = this.inboundNeighbors(Number(outletNodeId))
      .filter(nId => {
        const type = this.getNodeAttributes(Number(nId)).nodeType;
        return type === "job" || type === "root-job";
      });
    const sourceSpecNodeId = inboundIds[0];
    if (sourceSpecNodeId === undefined) return null;
    const sourceSpecNode = this.getNodeAttributes(Number(sourceSpecNodeId)) as (JobNode | RootJobNode);
    return { origin: sourceSpecNode, outletNode };
  }

  public getNodesConnectedToStream(streamNodeId: number | string) {
    const source = this.getSourceSpecNodeConnectedToStream(streamNodeId);
    const targets = this.getTargetSpecNodesConnectedToStream(streamNodeId);
    return { source, targets };
  }

  public toJSON() {
    const json = this.graph.toJSON();
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

  static async loadFromJSON(json: ReturnType<InstantiatedGraphOld["toJSON"]>) {
    const g = new InstantiatedGraphOld({
      contextId: json.contextId,
      defGraph: await loadDefGraphFromJson(json.defGraph),
      rootJobId: json.rootJobId,
      streamIdOverrides: json.streamIdOverrides,
      inletHasTransformOverridesByTag: json.inletHasTransformOverridesByTag,
      streamSourceSpecTypeByStreamId: json.streamSourceSpecTypeByStreamId,
    });
    await g.initPromise;
    return g;
  }
}


export const initInstantiatedGraph = ({
  contextId,
  defGraph,
  rootJobId,
  streamIdOverrides,
  inletHasTransformOverridesByTag,
  streamSourceSpecTypeByStreamId,
}: {
  contextId: string;
  defGraph: DefGraph;
  rootJobId: string;
  streamIdOverrides: StreamIdOverridesForRootSpec;
  inletHasTransformOverridesByTag: Record<string, boolean>;
  streamSourceSpecTypeByStreamId: Record<string, { specName: string; tag: string }>;
}) => {
  ensureNodeJSOnly();
  const { InstantiatedGraph } = require("./wasm/livestack_shared_wasm_nodejs");
  const defGraphJson = defGraph.toJson();

  const instantiatedGraph: InstantiatedGraph = new InstantiatedGraph(
    contextId,
    defGraphJson,
    rootJobId,
    streamIdOverrides,
    inletHasTransformOverridesByTag,
    streamSourceSpecTypeByStreamId,
  );
  return instantiatedGraph;
};

export type StreamIdOverridesForRootSpec = {
  [k: `${"in" | "out"}/${string}`]: string;
};
