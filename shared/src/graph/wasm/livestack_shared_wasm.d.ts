/* tslint:disable */
/* eslint-disable */
/**
* @param {string} json
* @returns {DefGraph}
*/
export function loadDefGraphFromJson(json: string): DefGraph;
/**
* @param {string} spec_name
* @param {string | undefined} [unique_spec_label]
* @returns {string}
*/
export function genSpecIdentifier(spec_name: string, unique_spec_label?: string): string;
/**
* @param {UniqueStreamIdentifierParams} p
* @returns {string}
*/
export function uniqueStreamIdentifier(p: UniqueStreamIdentifierParams): string;
export type NodeType = "RootSpec" | "Spec" | "StreamDef" | "Inlet" | "Outlet" | "Alias";

export interface DefGraphParams {
    root: DefGraphSpecParams;
}

export interface DefGraphSpecParams {
    name: string;
    inputTags: string[];
    outputTags: string[];
}

export interface SpecTagInfoParams {
    specName: string;
    uniqueSpecLabel: string | null;
    tag: string;
}

export interface SpecAndTagInfoAndDirection {
    specName: string;
    tag: string;
    uniqueSpecLabel: string | null;
    direction: string;
}

export interface DefGraphNode {
    id: number;
    nodeType: NodeType;
    specName: string | null;
    uniqueSpecLabel: string | null;
    tag: string | null;
    hasTransform: boolean | null;
    streamDefId: string | null;
    alias: string | null;
    direction: string | null;
    label: string;
}

export interface UniqueStreamIdentifierParams {
    from: SpecTagInfoParams | null;
    to: SpecTagInfoParams | null;
}

export interface GetInboundNodeSetsResultSingle {
    inletNode: DefGraphNode;
    streamNode: DefGraphNode;
}

export interface GetInboundNodeSetsResult {
    results: GetInboundNodeSetsResultSingle[];
}

export interface GetOutboundNodeSetsResultSingle {
    outletNode: DefGraphNode;
    streamNode: DefGraphNode;
}

export interface GetOutboundNodeSetsResult {
    results: GetOutboundNodeSetsResultSingle[];
}

export interface RawEdge {
    source: number;
    target: number;
}

export interface EdgesResults {
    results: RawEdge[];
}

export interface FromSpecAndTag {
    specName: string;
    output: string;
    uniqueSpecLabel: string | null;
}

export interface ToSpecAndTag {
    specName: string;
    input: string;
    hasTransform: boolean;
    uniqueSpecLabel: string | null;
}

export interface SpecAndTag {
    specName: string;
    tag: string;
    uniqueSpecLabel: string | null;
}

export interface AssignAliasParams {
    alias: string;
    tag: string;
    specName: string;
    uniqueSpecLabel: string | null;
    direction: string;
    rootSpecName: string;
}

export interface LookUpRootSpecAliasParams {
    specName: string;
    tag: string;
    uniqueSpecLabel: string | null;
    direction: string;
}

/**
*/
export class DefGraph {
  free(): void;
/**
* @param {DefGraphParams} params
*/
  constructor(params: DefGraphParams);
/**
* @returns {string}
*/
  toJson(): string;
/**
* @returns {Uint32Array}
*/
  getSpecNodeIds(): Uint32Array;
/**
* @returns {number}
*/
  getRootSpecNodeId(): number;
/**
* @param {number} node_id
* @returns {DefGraphNode}
*/
  getNodeAttributes(node_id: number): DefGraphNode;
/**
* @param {number} node_id
* @returns {GetInboundNodeSetsResult}
*/
  getInboundStreamNodes(node_id: number): GetInboundNodeSetsResult;
/**
* @param {number} node_id
* @returns {GetOutboundNodeSetsResult}
*/
  getOutboundStreamNodes(node_id: number): GetOutboundNodeSetsResult;
/**
* @returns {Uint32Array}
*/
  nodes(): Uint32Array;
/**
* @param {number} node_id
* @returns {Uint32Array}
*/
  inboundNeighbors(node_id: number): Uint32Array;
/**
* @param {number} node_id
* @returns {Uint32Array}
*/
  outboundNeighbors(node_id: number): Uint32Array;
/**
* @returns {EdgesResults}
*/
  edges(): EdgesResults;
/**
* @param {FromSpecAndTag} from
* @param {ToSpecAndTag} to
*/
  addConnectedDualSpecs(from: FromSpecAndTag, to: ToSpecAndTag): void;
/**
* @param {SpecAndTag} s
*/
  ensureOutletAndStream(s: SpecAndTag): void;
/**
* @param {SpecAndTag} s
* @param {boolean} has_transform
*/
  ensureInletAndStream(s: SpecAndTag, has_transform: boolean): void;
/**
* @param {AssignAliasParams} p
*/
  assignAlias(p: AssignAliasParams): void;
/**
* @returns {Uint32Array}
*/
  getAllAliasNodeIds(): Uint32Array;
/**
* @param {LookUpRootSpecAliasParams} p
* @returns {string | undefined}
*/
  lookupRootSpecAlias(p: LookUpRootSpecAliasParams): string | undefined;
/**
* @param {string} alias
* @param {string} direction
* @returns {SpecAndTagInfoAndDirection | undefined}
*/
  lookupSpecAndTagByAlias(alias: string, direction: string): SpecAndTagInfoAndDirection | undefined;
/**
* @param {number} stream_node_id
* @returns {any}
*/
  getNodesConnectedToStream(stream_node_id: number): any;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
  readonly memory: WebAssembly.Memory;
  readonly __wbg_defgraph_free: (a: number) => void;
  readonly defgraph_new: (a: number, b: number) => void;
  readonly defgraph_toJson: (a: number, b: number) => void;
  readonly defgraph_getSpecNodeIds: (a: number, b: number) => void;
  readonly defgraph_getRootSpecNodeId: (a: number) => number;
  readonly defgraph_getNodeAttributes: (a: number, b: number) => number;
  readonly defgraph_getInboundStreamNodes: (a: number, b: number) => number;
  readonly defgraph_getOutboundStreamNodes: (a: number, b: number) => number;
  readonly defgraph_nodes: (a: number, b: number) => void;
  readonly defgraph_inboundNeighbors: (a: number, b: number, c: number) => void;
  readonly defgraph_outboundNeighbors: (a: number, b: number, c: number) => void;
  readonly defgraph_edges: (a: number) => number;
  readonly defgraph_addConnectedDualSpecs: (a: number, b: number, c: number) => void;
  readonly defgraph_ensureOutletAndStream: (a: number, b: number) => void;
  readonly defgraph_ensureInletAndStream: (a: number, b: number, c: number) => void;
  readonly defgraph_assignAlias: (a: number, b: number) => void;
  readonly defgraph_getAllAliasNodeIds: (a: number, b: number) => void;
  readonly defgraph_lookupRootSpecAlias: (a: number, b: number, c: number) => void;
  readonly defgraph_lookupSpecAndTagByAlias: (a: number, b: number, c: number, d: number, e: number) => number;
  readonly defgraph_getNodesConnectedToStream: (a: number, b: number) => number;
  readonly loadDefGraphFromJson: (a: number, b: number) => number;
  readonly genSpecIdentifier: (a: number, b: number, c: number, d: number, e: number) => void;
  readonly uniqueStreamIdentifier: (a: number, b: number) => void;
  readonly __wbindgen_malloc: (a: number, b: number) => number;
  readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
  readonly __wbindgen_add_to_stack_pointer: (a: number) => number;
  readonly __wbindgen_free: (a: number, b: number, c: number) => void;
  readonly __wbindgen_exn_store: (a: number) => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;
/**
* Instantiates the given `module`, which can either be bytes or
* a precompiled `WebAssembly.Module`.
*
* @param {SyncInitInput} module
*
* @returns {InitOutput}
*/
export function initSync(module: SyncInitInput): InitOutput;

/**
* If `module_or_path` is {RequestInfo} or {URL}, makes a request and
* for everything else, calls `WebAssembly.instantiate` directly.
*
* @param {InitInput | Promise<InitInput>} module_or_path
*
* @returns {Promise<InitOutput>}
*/
export default function __wbg_init (module_or_path?: InitInput | Promise<InitInput>): Promise<InitOutput>;
