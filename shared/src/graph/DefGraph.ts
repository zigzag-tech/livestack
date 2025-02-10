const isBrowser = typeof window !== "undefined";
const pkgP = isBrowser ? import("./wasm/livestack_shared_wasm.js") : null;

import type { DefGraph } from "./wasm/livestack_shared_wasm_nodejs";
export type { DefGraph } from "./wasm/livestack_shared_wasm_nodejs";

export async function ensureInit() {
  const pkg = await pkgP;
  if (isBrowser) {
    await (pkg!.default as any)();
  } else {
    // await (pkg as any)();
  }
}

export function ensureNodeJSOnly() {
  if (isBrowser) {
    throw new Error("This function is only available in Node.js");
  }
}

export const loadDefGraphFromJson = async (json: string) => {
  if (isBrowser) {
    await ensureInit();
    return (await pkgP)!.loadDefGraphFromJson(json);
  } else {
    const {
      loadDefGraphFromJson,
    } = require("./wasm/livestack_shared_wasm_nodejs");
    return loadDefGraphFromJson(json) as DefGraph;
  }
};

export const genSpecIdentifier = async (name: string, uniqueLabel?: string) => {
  if (isBrowser) {
    await ensureInit();
    return (await pkgP!).genSpecIdentifier(name, uniqueLabel);
  } else {
    const {
      genSpecIdentifier,
    } = require("./wasm/livestack_shared_wasm_nodejs");
    return genSpecIdentifier(name, uniqueLabel) as string;
  }
};

export const initDefGraph = ({
  root,
}: {
  root: {
    name: string;
    inputTags: string[];
    outputTags: string[];
  };
}) => {
  ensureNodeJSOnly();
  const { DefGraph } = require("./wasm/livestack_shared_wasm_nodejs");
  const d: DefGraph = new DefGraph({
    root: {
      name: root.name,
      inputTags: root.inputTags,
      outputTags: root.outputTags,
    },
  });
  return d;
};

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
  hasTransform: boolean;
};
export type StreamDefNode = {
  nodeType: "stream-def";
  streamDefId: string;
};
export type AliasNode = {
  nodeType: "alias";
  alias: string;
  direction: "in" | "out";
};

export type DefGraphNode = { label: string } & (
  | RootSpecNode
  | SpecNode
  | StreamDefNode
  | InletNode
  | OutletNode
  | AliasNode
);
export type InferNodeData<T extends DefGraphNode["nodeType"]> = Extract<
  DefGraphNode,
  { nodeType: T }
>;

type CanonicalConnectionBase = {
  specName: string;
  uniqueSpecLabel?: string;
};

export type CanonicalConnectionFrom = CanonicalConnectionBase & {
  output: string;
};

export type CanonicalConnectionTo = CanonicalConnectionBase & {
  input: string;
  // inlets may have a transform
  hasTransform: boolean;
};

export type DefNodeType = DefGraphNode["nodeType"];
export type TransformFunction<T1 = any, T2 = any> = (o: T1) => T2 | Promise<T2>;
