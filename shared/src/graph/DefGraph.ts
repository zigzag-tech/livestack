const isBrowser = typeof window !== "undefined";
const pkgP = isBrowser ? import("./wasm/livestack_shared_wasm") : null;

import type { DefGraph } from "./wasm/livestack_shared_wasm_nodejs";
export type { DefGraph } from "./wasm/livestack_shared_wasm_nodejs";

async function ensureInit() {
  const pkg = await pkgP;
  if (isBrowser) {
    await (pkg!.default as any)();
  } else {
    // await (pkg as any)();
  }
}

function ensureNodeJSOnly() {
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
  nodeType: "Spec";
  specName: string;
  uniqueSpecLabel?: string;
};
export type RootSpecNode = {
  nodeType: "RootSpec";
  specName: string;
};

export type OutletNode = {
  nodeType: "Outlet";
  tag: string;
};

export type InletNode = {
  nodeType: "Inlet";
  tag: string;
  hasTransform: boolean;
};
export type StreamDefNode = {
  nodeType: "StreamDef";
  streamDefId: string;
};
export type AliasNode = {
  nodeType: "Alias";
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

type SpecBase = {
  name: string;
  input: { tags: (string | number | symbol)[] };
  output: { tags: (string | number | symbol)[] };
};

// class DefGraph_ extends Graph<DefGraphNode> {
//   private streamNodeIdBySpecIdentifierTypeAndTag: {
//     [k: `${string}::${"in" | "out"}/${string}`]: string;
//   } = {};

//   constructor({ root }: { root: SpecBase }) {
//     super({ multi: false });
//     this.addSingleRootSpec({ root });
//   }

//   private addSingleRootSpec({ root }: { root: SpecBase }) {
//     const specIdentifier = genSpecIdentifier(root.name);

//     const specNodeId = this.ensureNode(specIdentifier, {
//       nodeType: NodeType.RootSpec,
//       specName: root.name,
//       label: specIdentifier,
//     });

//     // add inlet and outlet nodes, their edges, and the connected stream node and edges
//     for (const tag of root.inputDefSet.tags) {
//       const tagStr = tag.toString();
//       const inletIdentifier = `${specIdentifier}/${tagStr}`;
//       const inletNodeId = this.ensureNode(inletIdentifier, {
//         nodeType: NodeType.Inlet,
//         tag: tagStr,
//         label: inletIdentifier,
//         // this might be overwritten when instantiated
//         hasTransform: false,
//       });

//       this.ensureEdge(inletNodeId, specNodeId);

//       const streamDefId =
//         this.streamNodeIdBySpecIdentifierTypeAndTag[
//           `${specIdentifier}::in/${tagStr}`
//         ] ||
//         uniqueStreamIdentifier({
//           to: {
//             specName: root.name,
//             uniqueSpecLabel: undefined,
//             tag: tagStr,
//           },
//         });

//       const streamNodeId = this.ensureNode(streamDefId, {
//         nodeType: NodeType.StreamDef,
//         label: streamDefId,
//         streamDefId,
//       });

//       this.ensureEdge(streamNodeId, inletNodeId);
//     }

//     for (const tag of root.outputDefSet.tags) {
//       const tagStr = tag.toString();
//       const outletIdentifier = `${specIdentifier}/${tagStr}`;
//       const outletNodeId = this.ensureNode(outletIdentifier, {
//         nodeType: NodeType.Outlet,
//         tag: tagStr,
//         label: outletIdentifier,
//       });

//       this.ensureEdge(specNodeId, outletNodeId);

//       const streamDefId =
//         this.streamNodeIdBySpecIdentifierTypeAndTag[
//           `${specIdentifier}::out/${tagStr}`
//         ] ||
//         uniqueStreamIdentifier({
//           from: {
//             specName: root.name,
//             uniqueSpecLabel: undefined,
//             tag: tagStr,
//           },
//         });

//       const streamNodeId = this.ensureNode(streamDefId, {
//         nodeType: NodeType.StreamDef,
//         label: streamDefId,
//         streamDefId,
//       });

//       this.ensureEdge(outletNodeId, streamNodeId);
//     }
//   }

//   public assignAlias({
//     alias,
//     specName,
//     rootSpecName,
//     uniqueSpecLabel,
//     type,
//     tag,
//   }: {
//     alias: string;
//     specName: string;
//     rootSpecName: string;
//     uniqueSpecLabel?: string;
//     type: "in" | "out";
//     tag: string | symbol | number;
//   }) {
//     const specNodeId = this.findNode((nId) => {
//       const attrs = this.getNodeAttributes(nId);
//       return (
//         attrs.nodeType === NodeType.Spec &&
//         attrs.specName === specName &&
//         attrs.uniqueSpecLabel === uniqueSpecLabel
//       );
//     });

//     if (!specNodeId) {
//       throw new Error(
//         `Spec node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}`
//       );
//     }

//     const rootSpecNodeId = this.findNode((nId) => {
//       const attrs = this.getNodeAttributes(nId);
//       return (
//         attrs.nodeType === NodeType.RootSpec && attrs.specName === rootSpecName
//       );
//     });
//     if (!rootSpecNodeId) {
//       throw new Error(`Root spec node not found for specName: ${rootSpecName}`);
//     }

//     const rootSpecNode = this.getNodeAttributes(rootSpecNodeId) as RootSpecNode;
//     const aliasId = `${rootSpecNode.specName}/${alias}`;

//     const aliasNodeId = this.ensureNode(alias, {
//       nodeType: NodeType.Alias,
//       alias,
//       label: aliasId,
//       direction: type,
//     });

//     if (type === "in") {
//       const inletNodeId = this.findInboundNeighbor(
//         specNodeId,
//         (nId, attrs) =>
//           attrs.nodeType === NodeType.Inlet && attrs.tag === tag.toString()
//       );

//       if (!inletNodeId) {
//         throw new Error(
//           `Inlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag.toString()}`
//         );
//       }
//       this.ensureEdge(inletNodeId, aliasNodeId);
//       this.ensureEdge(aliasNodeId, rootSpecNodeId);
//     } else {
//       const outletNodeId = this.findOutboundNeighbor(
//         specNodeId,p
//         (nId, attrs) =>
//           attrs.nodeType === NodeType.Outlet && attrs.tag === tag.toString()
//       );

//       if (!outletNodeId) {
//         throw new Error(
//           `Outlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag.toString()}`
//         );
//       }
//       this.ensureEdge(rootSpecNodeId, aliasNodeId);
//       this.ensureEdge(aliasNodeId, outletNodeId);
//     }
//   }

//   public lookupRootSpecAlias({
//     specName,
//     tag,
//     uniqueSpecLabel,
//     type,
//   }: {
//     specName: string;
//     tag: string;
//     uniqueSpecLabel?: string;
//     type: "in" | "out";
//   }) {
//     const specNodeId = this.findNode((nId) => {
//       const attrs = this.getNodeAttributes(nId);
//       return (
//         attrs.nodeType === "spec" &&
//         attrs.specName === specName &&
//         attrs.uniqueSpecLabel === uniqueSpecLabel
//       );
//     });

//     if (type === "in") {
//       const inletNodeId = this.findInboundNeighbor(
//         specNodeId,
//         (nId, attrs) => attrs.nodeType === "inlet" && attrs.tag === tag
//       );
//       if (!inletNodeId) {
//         throw new Error(
//           `Inlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag}`
//         );
//       }

//       const aliasNodeId = this.findOutboundNeighbor(
//         inletNodeId,
//         (nId, attrs) => attrs.nodeType === "alias"
//       );

//       return aliasNodeId
//         ? (this.getNodeAttributes(aliasNodeId) as AliasNode).alias
//         : null;
//     } else {
//       const outletNodeId = this.findOutboundNeighbor(
//         specNodeId,
//         (nId, attrs) => attrs.nodeType === "outlet" && attrs.tag === tag
//       );
//       if (!outletNodeId) {
//         throw new Error(
//           `Outlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag}`
//         );
//       }

//       const aliasNodeId = this.findInboundNeighbor(
//         outletNodeId,
//         (nId, attrs) => attrs.nodeType === "alias"
//       );

//       return aliasNodeId
//         ? (this.getNodeAttributes(aliasNodeId) as AliasNode).alias
//         : null;
//     }
//   }

//   public getAllAliasNodeIds() {
//     return this.filterNodes((nId, attrs) => attrs.nodeType === "alias");
//   }

//   public lookupSpecAndTagByAlias({
//     type,
//     alias,
//   }: {
//     type: "in" | "out";
//     alias: string | symbol | number;
//   }) {
//     const rootSpecNodeId = this.findNode((nId) => {
//       const attrs = this.getNodeAttributes(nId);
//       return attrs.nodeType === NodeType.RootSpec;
//     });

//     if (!rootSpecNodeId) {
//       throw new Error("Root spec node not found");
//     }

//     if (type === "in") {
//       const aliasNodeId = this.findInboundNeighbor(
//         rootSpecNodeId,
//         (nId, attrs) =>
//           attrs.nodeType === "alias" && attrs.alias === alias.toString()
//       );

//       if (!aliasNodeId) {
//         throw new Error(
//           `inbound alias node "${alias.toString()}" not found for ${rootSpecNodeId}. Available aliases: [${this.filterInboundNeighbors(
//             rootSpecNodeId,
//             (nId, attrs) => attrs.nodeType === "alias"
//           )
//             .map(
//               (nId) => `"${(this.getNodeAttributes(nId) as AliasNode).alias}"`
//             )
//             .join(", ")}]`
//         );
//       }
//       const inletNodeId = this.findInboundNeighbor(
//         aliasNodeId,
//         (nId, attrs) => attrs.nodeType === "inlet"
//       );
//       const inletNode = this.getNodeAttributes(inletNodeId) as InletNode;

//       const specNodeId = this.findOutboundNeighbor(
//         inletNodeId,
//         (nId, attrs) => attrs.nodeType === "spec"
//       );
//       const specNode = this.getNodeAttributes(specNodeId) as SpecNode;

//       return {
//         specName: specNode.specName,
//         tag: inletNode.tag,
//         uniqueSpecLabel: specNode.uniqueSpecLabel,
//         type,
//       };
//     } else {
//       const aliasNodeId = this.findOutboundNeighbor(
//         rootSpecNodeId,
//         (nId, attrs) =>
//           attrs.nodeType === "alias" && attrs.alias === alias.toString()
//       );

//       if (!aliasNodeId) {
//         throw new Error(
//           `Outbound alias node "${alias.toString()}" not found for ${rootSpecNodeId}. Available aliases: [${this.filterOutboundNeighbors(
//             rootSpecNodeId,
//             (nId, attrs) => attrs.nodeType === "alias"
//           )
//             .map(
//               (nId) => `"${(this.getNodeAttributes(nId) as AliasNode).alias}"`
//             )
//             .join(", ")}]`
//         );
//       }
//       const outletNodeId = this.findOutboundNeighbor(
//         aliasNodeId,
//         (nId, attrs) => attrs.nodeType === "outlet"
//       );
//       const outletNode = this.getNodeAttributes(outletNodeId) as OutletNode;

//       const specNodeId = this.findInboundNeighbor(
//         outletNodeId,
//         (nId, attrs) => attrs.nodeType === "spec"
//       );
//       const specNode = this.getNodeAttributes(specNodeId) as SpecNode;

//       return {
//         specName: specNode.specName,
//         tag: outletNode.tag,
//         uniqueSpecLabel: specNode.uniqueSpecLabel,
//         type,
//       };
//     }
//   }

//   public ensureInletAndStream({
//     specName,
//     uniqueSpecLabel,
//     tag,
//     hasTransform,
//   }: {
//     tag: string | symbol | number;
//     specName: string;
//     uniqueSpecLabel?: string;
//     hasTransform: boolean;
//   }) {
//     const specIdentifier = genSpecIdentifier(specName, uniqueSpecLabel);

//     const specNodeId = this.ensureNode(specIdentifier, {
//       specName,
//       ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
//       nodeType: "spec",
//       label: specIdentifier,
//     });

//     const tagStr = tag.toString();
//     const inletIdentifier = `${specIdentifier}/${tagStr}`;
//     const inletNodeId = this.ensureNode(inletIdentifier, {
//       nodeType: "inlet",
//       tag: tagStr,
//       label: inletIdentifier,
//       hasTransform,
//     });

//     this.ensureEdge(inletNodeId, specNodeId);

//     const streamDefId =
//       this.streamNodeIdBySpecIdentifierTypeAndTag[
//         `${specIdentifier}::in/${tagStr}`
//       ] ||
//       uniqueStreamIdentifier({
//         to: {
//           specName,
//           uniqueSpecLabel,
//           tag: tagStr,
//         },
//       });

//     const streamNodeId = this.ensureNode(streamDefId, {
//       nodeType: NodeType.StreamDef,
//       label: streamDefId,
//       streamDefId,
//     });

//     this.ensureEdge(streamNodeId, inletNodeId);
//   }

//   public ensureOutletAndStream({
//     specName,
//     uniqueSpecLabel,
//     tag,
//   }: {
//     specName: string;
//     uniqueSpecLabel?: string;
//     tag: string | symbol | number;
//   }) {
//     const specIdentifier = genSpecIdentifier(specName, uniqueSpecLabel);

//     const specNodeId = this.ensureNode(specIdentifier, {
//       specName,
//       ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
//       nodeType: "spec",
//       label: specIdentifier,
//     });

//     const tagStr = tag.toString();
//     const outletIdentifier = `${specIdentifier}/${tagStr}`;
//     const outletNodeId = this.ensureNode(outletIdentifier, {
//       nodeType: "outlet",
//       tag: tagStr,
//       label: outletIdentifier,
//     });

//     this.ensureEdge(specNodeId, outletNodeId);

//     const streamDefId =
//       this.streamNodeIdBySpecIdentifierTypeAndTag[
//         `${specIdentifier}::out/${tagStr}`
//       ] ||
//       uniqueStreamIdentifier({
//         from: {
//           specName,
//           uniqueSpecLabel,
//           tag: tagStr,
//         },
//       });

//     const streamNodeId = this.ensureNode(streamDefId, {
//       nodeType: NodeType.StreamDef,
//       label: streamDefId,
//       streamDefId,
//     });

//     this.ensureEdge(outletNodeId, streamNodeId);
//   }

//   public addConnectedDualSpecs(
//     from: CanonicalConnectionFrom,
//     to: CanonicalConnectionTo
//   ) {
//     const fromSpecIdentifier = genSpecIdentifier(
//       from.specName,
//       from.uniqueSpecLabel
//     );

//     const fromSpecNodeId = this.ensureNode(fromSpecIdentifier, {
//       specName: from.specName,
//       ...(from.uniqueSpecLabel
//         ? { uniqueSpecLabel: from.uniqueSpecLabel }
//         : {}),
//       nodeType: "spec",
//       label: fromSpecIdentifier,
//     });
//     const fromOutletNodeId = this.ensureNode(
//       `${fromSpecIdentifier}/${from.output}`,
//       {
//         nodeType: "outlet",
//         tag: from.output,
//         label: `${fromSpecIdentifier}/${from.output}`,
//       }
//     );

//     let streamNodeId: string;
//     let streamDefId: string;

//     // check existing stream def node
//     const existingStreamDefId = this.outboundNeighbors(fromOutletNodeId);
//     if (existingStreamDefId.length > 1) {
//       throw new Error(
//         `Expected exactly one stream def node, got ${existingStreamDefId.length}.`
//       );
//     } else if (existingStreamDefId.length === 1) {
//       // stream def node already exists; reuse it
//       streamNodeId = existingStreamDefId[0];
//       streamDefId = (this.getNodeAttributes(streamNodeId) as StreamDefNode)
//         .streamDefId;
//     } else {
//       streamDefId = uniqueStreamIdentifier({
//         from: {
//           specName: from.specName,
//           uniqueSpecLabel: from.uniqueSpecLabel,
//           tag: from.output,
//         },
//         to: {
//           specName: to.specName,
//           uniqueSpecLabel: to.uniqueSpecLabel,
//           tag: to.input,
//         },
//       });

//       streamNodeId = this.ensureNode(streamDefId, {
//         nodeType: NodeType.StreamDef,
//         label: streamDefId,
//         streamDefId,
//       });

//       this.ensureEdge(fromSpecNodeId, fromOutletNodeId);
//       this.ensureEdge(fromOutletNodeId, streamNodeId);
//     }
//     const toSpecIdentifier = genSpecIdentifier(to.specName, to.uniqueSpecLabel);
//     const id = `${toSpecIdentifier}/${to.input}`;
//     const toUniqueLabel = to.uniqueSpecLabel;
//     const toInletNodeId = this.ensureNode(id, {
//       nodeType: "inlet",
//       tag: to.input,
//       label: id,
//       hasTransform: !!to.hasTransform,
//     });

//     const toSpecNodeId = this.ensureNode(toSpecIdentifier, {
//       specName: to.specName,
//       ...(toUniqueLabel ? { uniqueSpecLabel: toUniqueLabel } : {}),
//       nodeType: "spec",
//       label: toSpecIdentifier,
//     });

//     this.ensureEdge(streamNodeId, toInletNodeId);
//     this.ensureEdge(toInletNodeId, toSpecNodeId);

//     this.streamNodeIdBySpecIdentifierTypeAndTag[
//       `${fromSpecIdentifier}::out/${from.output}`
//     ] = streamDefId;
//     this.streamNodeIdBySpecIdentifierTypeAndTag[
//       `${toSpecIdentifier}::in/${to.input}`
//     ] = streamDefId;

//     return {
//       fromSpecNodeId,
//       toSpecNodeId,
//       streamNodeId,
//       fromOutletNodeId,
//       toInletNodeId,
//     };
//   }

//   public ensureEdge(from: string, to: string) {
//     if (!super.hasEdge(from, to)) {
//       super.addEdge(from, to);
//     }
//   }

//   public ensureNode<T extends NodeType>(
//     id: string,
//     data: InferNodeData<T>
//   ): string {
//     const nodeId = `${data.nodeType}_${id}`;
//     if (!this.hasNode(nodeId)) {
//       this.addNode(nodeId, { ...data });
//     }
//     return nodeId;
//   }

//   public getRootSpecNodeId(): string {
//     const nodes = this.filterNodes(
//       (_, attrs) => attrs.nodeType === NodeType.RootSpec
//     );
//     if (nodes.length !== 1) {
//       throw new Error(
//         `Expected exactly one root spec node, got ${nodes.length}.`
//       );
//     }
//     return nodes[0];
//   }

//   public getSpecNodeIds() {
//     return this.filterNodes((_, attrs) => attrs.nodeType === "spec");
//   }

//   public getInboundNodeSets(specNodeId: string) {
//     const inletNodeIds = this.filterInboundNeighbors(
//       specNodeId,
//       (n, attrs) => attrs.nodeType === "inlet"
//     );

//     return inletNodeIds.map((inletNodeId) => {
//       const streamNodeId = this.filterInboundNeighbors(
//         inletNodeId,
//         (nid) => this.getNodeAttributes(nid).nodeType === NodeType.StreamDef
//       )[0];
//       return {
//         inletNode: {
//           ...(this.getNodeAttributes(inletNodeId) as InletNode),
//           id: inletNodeId,
//         },
//         streamNode: {
//           ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
//           id: streamNodeId,
//         },
//       };
//     });
//   }

//   public getOutboundNodeSets(specNodeId: string) {
//     const outletNodeIds = this.filterOutboundNeighbors(
//       specNodeId,
//       (n, attrs) => attrs.nodeType === "outlet"
//     );

//     return outletNodeIds.map((outletNodeId) => {
//       const streamNodeId = this.filterOutNeighbors(
//         outletNodeId,
//         (nid) => this.getNodeAttributes(nid).nodeType === NodeType.StreamDef
//       )[0];
//       return {
//         outletNode: {
//           ...(this.getNodeAttributes(outletNodeId) as OutletNode),
//           id: outletNodeId,
//         },
//         streamNode: {
//           ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
//           id: streamNodeId,
//         },
//       };
//     });
//   }

//   public override toJSON() {
//     const json = super.toJSON();
//     return {
//       ...json,
//       streamNodeIdBySpecIdentifierTypeAndTag:
//         this.streamNodeIdBySpecIdentifierTypeAndTag,
//     };
//   }
// }
export type TransformFunction<T1 = any, T2 = any> = (o: T1) => T2 | Promise<T2>;
