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
export type DefGraphNodeType = "root-spec" | "spec" | "stream-def" | "inlet" | "outlet" | "alias";

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
    nodeType: DefGraphNodeType;
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

export type InstantiatedNodeType = "root-job" | "job" | "stream" | "inlet" | "outlet" | "alias";

export interface InstantiatedGraphNodeWasm {
    nodeType: InstantiatedNodeType;
    jobId: string | null;
    specName: string | null;
    uniqueSpecLabel: string | null;
    streamId: string | null;
    tag: string | null;
    hasTransform: boolean | null;
    alias: string | null;
    direction: string | null;
    label: string;
}

export interface InstantiatedStreamConnectionSourceWasm {
    origin: InstantiatedGraphNodeWasm;
    outletNode: InstantiatedGraphNodeWasm;
}

export interface InstantiatedStreamConnectionTargetWasm {
    inletNode: InstantiatedGraphNodeWasm;
    destination: InstantiatedGraphNodeWasm;
}

export interface InstantiatedStreamConnectionsWasm {
    source: InstantiatedStreamConnectionSourceWasm | null;
    targets: InstantiatedStreamConnectionTargetWasm[];
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
* @returns {(number)[]}
*/
  getSpecNodeIds(): (number)[];
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
/**
* The main struct bridging Rust's InstantiatedGraphImpl to JS/Wasm.  
* Instead of receiving a DefGraph, it takes a serialized JSON string for the DefGraph.
*/
export class InstantiatedGraph {
  free(): void;
/**
* Create a new InstantiatedGraphWasm by:
*  - Deserializing a `DefGraph` from JSON,
*  - Parsing the stream/inlet overrides from JS objects,
*  - Constructing the internal Rust `InstantiatedGraph`.
* @param {string} context_id
* @param {string} def_graph_json
* @param {string} root_job_id
* @param {any} streamIdOverrides
* @param {any} inletHasTransformOverridesByTag
* @param {any} streamSourceSpecTypeByStreamId
*/
  constructor(context_id: string, def_graph_json: string, root_job_id: string, streamIdOverrides: any, inletHasTransformOverridesByTag: any, streamSourceSpecTypeByStreamId: any);
/**
* Retrieve all node IDs in the InstantiatedGraph (JS array of u32).
* @returns {(number)[]}
*/
  nodes(): (number)[];
/**
* Return the node's attributes as a JS object.  
* Similar to the TS `InstantiatedGraph.getNodeAttributes(...)`.
* @param {number} node_id
* @returns {InstantiatedGraphNodeWasm}
*/
  getNodeAttributes(node_id: number): InstantiatedGraphNodeWasm;
/**
* Return the total number of edges in the InstantiatedGraph (optional).
* @returns {number}
*/
  edgeCount(): number;
/**
* Retrieve the "source" and "targets" for a particular stream node,
* returning a JS object with shape `{ source: { origin, outlet_node }, targets: [...] }`.
* @param {number} stream_node_id
* @returns {any}
*/
  getNodesConnectedToStream(stream_node_id: number): any;
/**
* Return an array of node IDs that are inbound neighbors of the given node ID.
* @param {number} node_id
* @returns {Uint32Array}
*/
  inboundNeighbors(node_id: number): Uint32Array;
/**
* getSourceSpecNodeConnectedToStream
* @param {number} stream_node_id
* @returns {InstantiatedStreamConnectionSourceWasm | undefined}
*/
  getSourceSpecNodeConnectedToStream(stream_node_id: number): InstantiatedStreamConnectionSourceWasm | undefined;
/**
* Just like the TS version: find a stream node by matching a jobId, direction, and tag.
* @param {string} job_id
* @param {string} direction
* @param {string} tag
* @returns {number | undefined}
*/
  findStreamNodeIdConnectedToJob(job_id: string, direction: string, tag: string): number | undefined;
/**
* Return an array of edge IDs that enter the given nodeId.
* Mirrors InstantiatedGraph::inbound_edges.
* @param {number} node_id
* @returns {(number)[]}
*/
  inboundEdges(node_id: number): (number)[];
/**
* Return an array of edge IDs that leave the given nodeId.
* Mirrors InstantiatedGraph::outbound_edges.
* @param {number} node_id
* @returns {(number)[]}
*/
  outboundEdges(node_id: number): (number)[];
/**
* Return the source node ID of the given edge ID.
* @param {number} edge_id
* @returns {number}
*/
  source(edge_id: number): number;
/**
* Return the target node ID of the given edge ID.
* @param {number} edge_id
* @returns {number}
*/
  target(edge_id: number): number;
/**
* Return the serialized JSON string of the InstantiatedGraph.
* @returns {string}
*/
  toJson(): string;
/**
* Return an array of edge IDs.
* @returns {string}
*/
  edgesPrintout(): string;
/**
* Return the root job ID string, which is stored in the Rust InstantiatedGraph.
*/
  readonly rootJobId: string;
/**
* Return the root job node as a JS object.
*/
  readonly rootJobNodeId: number;
/**
* Return the root job ID string, which is stored in the Rust InstantiatedGraph.
*/
  readonly streamSourceSpecTypeByStreamId: any;
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
  readonly __wbg_instantiatedgraph_free: (a: number) => void;
  readonly instantiatedgraph_new: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number, j: number) => void;
  readonly instantiatedgraph_nodes: (a: number, b: number) => void;
  readonly instantiatedgraph_getNodeAttributes: (a: number, b: number) => number;
  readonly instantiatedgraph_edgeCount: (a: number) => number;
  readonly instantiatedgraph_getNodesConnectedToStream: (a: number, b: number, c: number) => void;
  readonly instantiatedgraph_inboundNeighbors: (a: number, b: number, c: number) => void;
  readonly instantiatedgraph_getSourceSpecNodeConnectedToStream: (a: number, b: number) => number;
  readonly instantiatedgraph_findStreamNodeIdConnectedToJob: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number) => void;
  readonly instantiatedgraph_inboundEdges: (a: number, b: number, c: number) => void;
  readonly instantiatedgraph_outboundEdges: (a: number, b: number, c: number) => void;
  readonly instantiatedgraph_source: (a: number, b: number) => number;
  readonly instantiatedgraph_target: (a: number, b: number) => number;
  readonly instantiatedgraph_rootJobId: (a: number, b: number) => void;
  readonly instantiatedgraph_streamSourceSpecTypeByStreamId: (a: number, b: number) => void;
  readonly instantiatedgraph_rootJobNodeId: (a: number) => number;
  readonly instantiatedgraph_toJson: (a: number, b: number) => void;
  readonly instantiatedgraph_edgesPrintout: (a: number, b: number) => void;
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
