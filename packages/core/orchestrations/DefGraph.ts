import { alias } from "@livestack/core";
import Graph from "graphology";
import { IOSpec } from "@livestack/shared";
import { Attributes } from "graphology-types";
import {
  uniqueSpecIdentifier,
  uniqueStreamIdentifier,
} from "./InstantiatedGraph";

export type SpecNode = {
  nodeType: "spec";
  specName: string;
  uniqueSpecLabel?: string;
};
export type RootSpecNode = {
  nodeType: "root-spec";
  specName: string;
};
type Transformable = {
  hasTransform: boolean;
};
export type OutletNode = Transformable & {
  nodeType: "outlet";
  tag: string;
};

export type InletNode = Transformable & {
  nodeType: "inlet";
  tag: string;
};
export type StreamDefNode = {
  nodeType: "stream-def";
  streamDefId: string;
};
export type AliasNode = {
  nodeType: "alias";
  alias: string;
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
  spec: IOSpec<any, any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec: string;
  transform?: Function;
};

export type CanonicalConnectionFrom = CanonicalConnectionBase & {
  tagInSpecType: "output";
};

export type CanonicalConnectionTo = CanonicalConnectionBase & {
  tagInSpecType: "input";
};

export type DefNodeType = DefGraphNode["nodeType"];
export class DefGraph extends Graph<DefGraphNode> {
  private streamNodeIdBySpecIdentifierTypeAndTag: {
    [k: `${string}::${"in" | "out"}/${string}`]: string;
  } = {};

  constructor({ root }: { root: IOSpec<any, any, any, any> }) {
    super({ multi: false });
    this.addSingleRootSpec(root);
  }

  private addSingleRootSpec(spec: IOSpec<any, any, any, any>) {
    const specIdentifier = uniqueSpecIdentifier({
      spec,
    });

    const specNodeId = this.ensureNode(specIdentifier, {
      nodeType: "root-spec",
      specName: spec.name,
      label: specIdentifier,
    });

    // add inlet and outlet nodes, their edges, and the connected stream node and edges
    for (const tag of spec.inputDefSet.keys) {
      const tagStr = tag.toString();
      const inletIdentifier = `${specIdentifier}/${tagStr}`;
      const inletNodeId = this.ensureNode(inletIdentifier, {
        nodeType: "inlet",
        tag: tagStr,
        label: inletIdentifier,
        hasTransform: false,
      });

      this.ensureEdge(inletNodeId, specNodeId);

      const streamDefId =
        this.streamNodeIdBySpecIdentifierTypeAndTag[
          `${specIdentifier}::in/${tagStr}`
        ] ||
        uniqueStreamIdentifier({
          to: {
            specName: spec.name,
            uniqueSpecLabel: undefined,
            tag: tagStr,
          },
        });

      const streamNodeId = this.ensureNode(streamDefId, {
        nodeType: "stream-def",
        label: streamDefId,
        streamDefId,
      });

      this.ensureEdge(streamNodeId, inletNodeId);
    }

    for (const tag of spec.outputDefSet.keys) {
      const tagStr = tag.toString();
      const outletIdentifier = `${specIdentifier}/${tagStr}`;
      const outletNodeId = this.ensureNode(outletIdentifier, {
        nodeType: "outlet",
        tag: tagStr,
        label: outletIdentifier,
        hasTransform: false,
      });

      this.ensureEdge(specNodeId, outletNodeId);

      const streamDefId =
        this.streamNodeIdBySpecIdentifierTypeAndTag[
          `${specIdentifier}::out/${tagStr}`
        ] ||
        uniqueStreamIdentifier({
          from: {
            specName: spec.name,
            uniqueSpecLabel: undefined,
            tag: tagStr,
          },
        });

      const streamNodeId = this.ensureNode(streamDefId, {
        nodeType: "stream-def",
        label: streamDefId,
        streamDefId,
      });

      this.ensureEdge(outletNodeId, streamNodeId);
    }
  }

  public assignAlias({
    alias,
    specName,
    rootSpecName,
    uniqueSpecLabel,
    type,
    tag,
  }: {
    alias: string;
    specName: string;
    rootSpecName: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
    tag: string | symbol | number;
  }) {
    const specNodeId = this.findNode((nId) => {
      const attrs = this.getNodeAttributes(nId);
      return (
        attrs.nodeType === "spec" &&
        attrs.specName === specName &&
        attrs.uniqueSpecLabel === uniqueSpecLabel
      );
    });

    if (!specNodeId) {
      throw new Error(
        `Spec node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}`
      );
    }

    const rootSpecNodeId = this.findNode((nId) => {
      const attrs = this.getNodeAttributes(nId);
      return attrs.nodeType === "root-spec" && attrs.specName === rootSpecName;
    });
    if (!rootSpecNodeId) {
      throw new Error(`Root spec node not found for specName: ${rootSpecName}`);
    }

    const rootSpecNode = this.getNodeAttributes(rootSpecNodeId) as RootSpecNode;
    const aliasId = `${rootSpecNode.specName}/${alias}`;

    const aliasNodeId = this.ensureNode(alias, {
      nodeType: "alias",
      alias,
      label: aliasId,
    });

    if (type === "in") {
      const inletNodeId = this.findInboundNeighbor(
        specNodeId,
        (nId, attrs) =>
          attrs.nodeType === "inlet" && attrs.tag === tag.toString()
      );

      if (!inletNodeId) {
        throw new Error(
          `Inlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag.toString()}`
        );
      }
      this.ensureEdge(inletNodeId, aliasNodeId);
      this.ensureEdge(aliasNodeId, rootSpecNodeId);
    } else {
      const outletNodeId = this.findOutboundNeighbor(
        specNodeId,
        (nId, attrs) =>
          attrs.nodeType === "outlet" && attrs.tag === tag.toString()
      );

      if (!outletNodeId) {
        throw new Error(
          `Outlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag.toString()}`
        );
      }
      this.ensureEdge(rootSpecNodeId, aliasNodeId);
      this.ensureEdge(aliasNodeId, outletNodeId);
    }
  }

  public lookupRootSpecAlias({
    specName,
    tag,
    uniqueSpecLabel,
    type,
  }: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
  }) {
    const rootSpecNodeId = this.findNode((nId) => {
      const attrs = this.getNodeAttributes(nId);
      return attrs.nodeType === "root-spec" && attrs.specName === specName;
    });

    const specNodeId = this.findNode((nId) => {
      const attrs = this.getNodeAttributes(nId);
      return (
        attrs.nodeType === "spec" &&
        attrs.specName === specName &&
        attrs.uniqueSpecLabel === uniqueSpecLabel
      );
    });

    if (type === "in") {
      const inletNodeId = this.findInboundNeighbor(
        specNodeId,
        (nId, attrs) => attrs.nodeType === "inlet" && attrs.tag === tag
      );
      if (!inletNodeId) {
        throw new Error(
          `Inlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag}`
        );
      }

      const aliasNodeId = this.findOutboundNeighbor(
        inletNodeId,
        (nId, attrs) => attrs.nodeType === "alias"
      );

      return aliasNodeId
        ? (this.getNodeAttributes(aliasNodeId) as AliasNode).alias
        : null;
    } else {
      const outletNodeId = this.findOutboundNeighbor(
        specNodeId,
        (nId, attrs) => attrs.nodeType === "outlet" && attrs.tag === tag
      );
      if (!outletNodeId) {
        throw new Error(
          `Outlet node not found for specName: ${specName}, uniqueSpecLabel: ${uniqueSpecLabel}, tag: ${tag}`
        );
      }

      const aliasNodeId = this.findInboundNeighbor(
        outletNodeId,
        (nId, attrs) => attrs.nodeType === "alias"
      );

      return aliasNodeId
        ? (this.getNodeAttributes(aliasNodeId) as AliasNode).alias
        : null;
    }
  }

  public lookupSpecAndTagByAlias({
    type,
    alias,
  }: {
    type: "in" | "out";
    alias: string | symbol | number;
  }) {
    const rootSpecNodeId = this.findNode((nId) => {
      const attrs = this.getNodeAttributes(nId);
      return attrs.nodeType === "root-spec";
    });

    if (!rootSpecNodeId) {
      throw new Error("Root spec node not found");
    }

    if (type === "in") {
      const aliasNodeId = this.findInboundNeighbor(
        rootSpecNodeId,
        (nId, attrs) =>
          attrs.nodeType === "alias" && attrs.alias === alias.toString()
      );
      const inletNodeId = this.findInboundNeighbor(
        aliasNodeId,
        (nId, attrs) => attrs.nodeType === "inlet"
      );
      const inletNode = this.getNodeAttributes(inletNodeId) as InletNode;

      const specNodeId = this.findOutboundNeighbor(
        inletNodeId,
        (nId, attrs) => attrs.nodeType === "spec"
      );
      const specNode = this.getNodeAttributes(specNodeId) as SpecNode;

      return {
        specName: specNode.specName,
        tag: inletNode.tag,
        uniqueSpecLabel: specNode.uniqueSpecLabel,
        type,
      };
    } else {
      const aliasNodeId = this.findOutboundNeighbor(
        rootSpecNodeId,
        (nId, attrs) =>
          attrs.nodeType === "alias" && attrs.alias === alias.toString()
      );

      if (!aliasNodeId) {
        throw new Error(`Alias node not found for alias: ${alias.toString()}`);
      }
      const outletNodeId = this.findOutboundNeighbor(
        aliasNodeId,
        (nId, attrs) => attrs.nodeType === "outlet"
      );
      const outletNode = this.getNodeAttributes(outletNodeId) as OutletNode;

      const specNodeId = this.findInboundNeighbor(
        outletNodeId,
        (nId, attrs) => attrs.nodeType === "spec"
      );
      const specNode = this.getNodeAttributes(specNodeId) as SpecNode;

      return {
        specName: specNode.specName,
        tag: outletNode.tag,
        uniqueSpecLabel: specNode.uniqueSpecLabel,
        type,
      };
    }
  }

  public ensureInletAndStream({
    specName,
    uniqueSpecLabel,
    tag,
    hasTransform,
  }: {
    tag: string | symbol | number;
    specName: string;
    uniqueSpecLabel?: string;
    hasTransform: boolean;
  }) {
    const specIdentifier = uniqueSpecIdentifier({
      specName,
      uniqueSpecLabel,
    });

    const specNodeId = this.ensureNode(specIdentifier, {
      specName,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      nodeType: "spec",
      label: specIdentifier,
    });

    const tagStr = tag.toString();
    const inletIdentifier = `${specIdentifier}/${tagStr}`;
    const inletNodeId = this.ensureNode(inletIdentifier, {
      nodeType: "inlet",
      tag: tagStr,
      label: inletIdentifier,
      hasTransform,
    });

    this.ensureEdge(inletNodeId, specNodeId);

    const streamDefId =
      this.streamNodeIdBySpecIdentifierTypeAndTag[
        `${specIdentifier}::in/${tagStr}`
      ] ||
      uniqueStreamIdentifier({
        to: {
          specName,
          uniqueSpecLabel,
          tag: tagStr,
        },
      });

    const streamNodeId = this.ensureNode(streamDefId, {
      nodeType: "stream-def",
      label: streamDefId,
      streamDefId,
    });

    this.ensureEdge(streamNodeId, inletNodeId);
  }

  public ensureOutletAndStream({
    specName,
    uniqueSpecLabel,
    tag,
  }: {
    specName: string;
    uniqueSpecLabel?: string;
    tag: string | symbol | number;
    hasTransform: boolean;
  }) {
    const specIdentifier = uniqueSpecIdentifier({
      specName,
      uniqueSpecLabel,
    });

    const specNodeId = this.ensureNode(specIdentifier, {
      specName,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      nodeType: "spec",
      label: specIdentifier,
    });

    const tagStr = tag.toString();
    const outletIdentifier = `${specIdentifier}/${tagStr}`;
    const outletNodeId = this.ensureNode(outletIdentifier, {
      nodeType: "outlet",
      tag: tagStr,
      label: outletIdentifier,
      hasTransform: false,
    });

    this.ensureEdge(specNodeId, outletNodeId);

    const streamDefId =
      this.streamNodeIdBySpecIdentifierTypeAndTag[
        `${specIdentifier}::out/${tagStr}`
      ] ||
      uniqueStreamIdentifier({
        from: {
          specName,
          uniqueSpecLabel,
          tag: tagStr,
        },
      });

    const streamNodeId = this.ensureNode(streamDefId, {
      nodeType: "stream-def",
      label: streamDefId,
      streamDefId,
    });

    this.ensureEdge(outletNodeId, streamNodeId);
  }

  public addConnectedDualSpecs(
    from: CanonicalConnectionFrom,
    to: CanonicalConnectionTo
  ) {
    const fromSpecIdentifier = uniqueSpecIdentifier(from);
    const fromSpecNodeId = this.ensureNode(fromSpecIdentifier, {
      specName: from.spec.name,
      ...(from.uniqueSpecLabel
        ? { uniqueSpecLabel: from.uniqueSpecLabel }
        : {}),
      nodeType: "spec",
      label: fromSpecIdentifier,
    });
    const fromOutletNodeId = this.ensureNode(
      `${fromSpecIdentifier}/${from.tagInSpec}`,
      {
        nodeType: "outlet",
        tag: from.tagInSpec,
        label: `${fromSpecIdentifier}/${from.tagInSpec}`,
        hasTransform: !!from.transform,
      }
    );

    const toSpecIdentifier = uniqueSpecIdentifier(to);
    const id = `${toSpecIdentifier}/${to.tagInSpec}`;
    const toInletNodeId = this.ensureNode(id, {
      nodeType: "inlet",
      tag: to.tagInSpec,
      label: id,
      hasTransform: !!from.transform,
    });
    const toUniqueLabel = to.uniqueSpecLabel;
    const toSpecNodeId = this.ensureNode(toSpecIdentifier, {
      specName: to.spec.name,
      ...(toUniqueLabel ? { uniqueSpecLabel: toUniqueLabel } : {}),
      nodeType: "spec",
      label: toSpecIdentifier,
    });

    const streamDefId = uniqueStreamIdentifier({
      from: {
        specName: from.spec.name,
        uniqueSpecLabel: from.uniqueSpecLabel,
        tag: from.tagInSpec,
      },
      to: {
        specName: to.spec.name,
        uniqueSpecLabel: to.uniqueSpecLabel,
        tag: to.tagInSpec,
      },
    });

    const streamNodeId = this.ensureNode(streamDefId, {
      nodeType: "stream-def",
      label: streamDefId,
      streamDefId,
    });

    this.ensureEdge(fromSpecNodeId, fromOutletNodeId);
    this.ensureEdge(fromOutletNodeId, streamNodeId);
    this.ensureEdge(streamNodeId, toInletNodeId);
    this.ensureEdge(toInletNodeId, toSpecNodeId);

    this.streamNodeIdBySpecIdentifierTypeAndTag[
      `${fromSpecIdentifier}::out/${from.tagInSpec}`
    ] = streamDefId;
    this.streamNodeIdBySpecIdentifierTypeAndTag[
      `${toSpecIdentifier}::in/${to.tagInSpec}`
    ] = streamDefId;

    return {
      fromSpecNodeId,
      toSpecNodeId,
      streamNodeId,
      fromOutletNodeId,
      toInletNodeId,
    };
  }

  public ensureParentChildRelation(
    parentSpecNodeId: string,
    childSpecNodeId: string
  ) {
    this.ensureEdge(childSpecNodeId, parentSpecNodeId, {
      type: "parent-child",
    });
  }

  private ensureEdge(from: string, to: string, attributes: Attributes = {}) {
    if (!super.hasEdge(from, to)) {
      super.addEdge(from, to, attributes);
    } else {
      const edge = this.getEdgeAttributes(from, to);
      for (const [k, v] of Object.entries(attributes)) {
        if (edge[k] !== v) {
          edge[k] = v;
        }
      }
    }
  }

  private ensureNode<T extends DefNodeType>(
    id: string,
    data: InferNodeData<T>
  ): string {
    const nodeId = `${data.nodeType}_${id}`;
    if (!this.hasNode(nodeId)) {
      this.addNode(nodeId, { ...data });
    }
    return nodeId;
  }

  public getRootSpecNodeId(): string {
    const nodes = this.filterNodes(
      (_, attrs) => attrs.nodeType === "root-spec"
    );
    if (nodes.length !== 1) {
      throw new Error(
        `Expected exactly one root spec node, got ${nodes.length}.`
      );
    }
    return nodes[0];
  }

  public getSpecNodeIds() {
    return this.filterNodes((_, attrs) => attrs.nodeType === "spec");
  }

  public getInboundNodeSets(specNodeId: string) {
    const inletNodeIds = this.filterInboundNeighbors(
      specNodeId,
      (n, attrs) => attrs.nodeType === "inlet"
    );

    return inletNodeIds.map((inletNodeId) => {
      const [ie] = this.inboundEdges(inletNodeId) as (string | undefined)[];
      const streamNodeId = this.source(ie);
      return {
        inletNode: {
          ...(this.getNodeAttributes(inletNodeId) as InletNode),
          id: inletNodeId,
        },
        streamNode: {
          ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
          id: streamNodeId,
        },
      };
    });
  }

  public getOutboundNodeSets(specNodeId: string) {
    const outletNodeIds = this.filterOutboundNeighbors(
      specNodeId,
      (n, attrs) => attrs.nodeType === "outlet"
    );

    return outletNodeIds.map((outletNodeId) => {
      const [oe] = this.outboundEdges(outletNodeId) as (string | undefined)[];
      const streamNodeId = this.target(oe);
      return {
        outletNode: {
          ...(this.getNodeAttributes(outletNodeId) as OutletNode),
          id: outletNodeId,
        },
        streamNode: {
          ...(this.getNodeAttributes(streamNodeId) as DefGraphNode),
          id: streamNodeId,
        },
      };
    });
  }
}
