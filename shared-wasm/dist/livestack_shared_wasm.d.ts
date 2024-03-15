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
    inputDefSet: DefSetParams;
    outputDefSet: DefSetParams;
}

export interface DefSetParams {
    tags: string[];
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
  getInboundNodeSets(node_id: number): GetInboundNodeSetsResult;
/**
* @param {number} node_id
* @returns {GetOutboundNodeSetsResult}
*/
  getOutboundNodeSets(node_id: number): GetOutboundNodeSetsResult;
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
}
