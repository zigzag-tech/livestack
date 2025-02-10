import {
  ensureNodeJSOnly
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
