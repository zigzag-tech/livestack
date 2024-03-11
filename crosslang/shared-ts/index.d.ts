/* tslint:disable */
/* eslint-disable */

/* auto-generated by NAPI-RS */

export function sumAsString(a: number, b: number): string
export function defGraph(rootSpecName: string, inputTags: Array<string>, outputTags: Array<string>): DefGraph
export class DefGraph {
  /** Deserializes a `DefGraph` from a JSON string. */
  static loadFromJson(jsonStr: string): DefGraph
  /** Serializes the `DefGraph` to a JSON string. */
  toJson(): string
  getAllAliasNodeIds(): Array<number>
  getRootSpecNodeId(): number | null
  constructor(rootSpecName: string, inputTags: Array<string>, outputTags: Array<string>)
}
