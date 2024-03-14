/* tslint:disable */
/* eslint-disable */

/* auto-generated by NAPI-RS */

export function genSpecIdentifier(specName: string, uniqueSpecLabel?: string | undefined | null): string
export interface SpecTagInfoParams {
  specName: string
  uniqueSpecLabel?: string
  tag: string
}
export interface UniqueStreamIdentifierParams {
  from?: SpecTagInfoParams
  to?: SpecTagInfoParams
}
export function uniqueStreamIdentifier(p: UniqueStreamIdentifierParams): string
export interface DefGraphSpecParams {
  name: string
  inputDefSet: DefSetParams
  outputDefSet: DefSetParams
}
export interface DefGraphParams {
  root: DefGraphSpecParams
}
export interface DefSetParams {
  tags: Array<string>
}
export const enum NodeType {
  RootSpec = 0,
  Spec = 1,
  StreamDef = 2,
  Inlet = 3,
  Outlet = 4,
  Alias = 5
}
export interface DefGraphNode {
  id: number
  nodeType: NodeType
  specName?: string
  uniqueSpecLabel?: string
  tag?: string
  hasTransform?: boolean
  streamDefId?: string
  alias?: string
  direction?: string
  label: string
}
export interface GetInboundNodeSetsResult {
  inletNode: DefGraphNode
  streamNode: DefGraphNode
}
export interface GetOutboundNodeSetsResult {
  outletNode: DefGraphNode
  streamNode: DefGraphNode
}
export interface RawEdge {
  source: number
  target: number
}
export interface FromSpecAndTag {
  specName: string
  output: string
  uniqueSpecLabel?: string
}
export interface ToSpecAndTag {
  specName: string
  input: string
  hasTransform: boolean
  uniqueSpecLabel?: string
}
export interface SpecAndTag {
  specName: string
  tag: string
  uniqueSpecLabel?: string
}
export interface AssignAliasParams {
  alias: string
  tag: string
  specName: string
  uniqueSpecLabel?: string
  direction: string
  rootSpecName: string
}
/**
const existingAlias = defG.lookupRootSpecAlias({
specName,
tag,
uniqueSpecLabel,
type,
});
*/
export interface LookUpRootSpecAliasParams {
  specName: string
  tag: string
  uniqueSpecLabel?: string
  direction: string
}
export interface SpecTagInfo {
  specName: string
  tag: string
  uniqueSpecLabel?: string
}
export interface SpecAndTagInfoAndDirection {
  specName: string
  tag: string
  uniqueSpecLabel?: string
  direction: string
}
export function loadDefGraphFromJson(json: string): DefGraph
export class DefGraph {
  constructor(p: DefGraphParams)
  toJson(): string
  getSpecNodeIds(): Array<number>
  getRootSpecNodeId(): number
  getNodeAttributes(nodeId: number): DefGraphNode
  getInboundNodeSets(nodeId: number): Array<GetInboundNodeSetsResult>
  getOutboundNodeSets(nodeId: number): Array<GetOutboundNodeSetsResult>
  nodes(): Array<number>
  inboundNeighbors(nodeId: number): Array<number>
  outboundNeighbors(nodeId: number): Array<number>
  edges(): Array<RawEdge>
  addConnectedDualSpecs(from: FromSpecAndTag, to: ToSpecAndTag): void
  ensureOutletAndStream(s: SpecAndTag): void
  ensureInletAndStream(s: SpecAndTag, hasTransform: boolean): void
  assignAlias(p: AssignAliasParams): void
  getAllAliasNodeIds(): Array<number>
  lookupRootSpecAlias(p: LookUpRootSpecAliasParams): string | null
  lookupSpecAndTagByAlias(alias: string, direction: string): SpecAndTagInfoAndDirection | null
}
