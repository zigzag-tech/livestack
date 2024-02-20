import { DefGraph } from "./DefGraph";

describe("DefGraph", () => {
  it("should create a root spec node", () => {
    const rootSpec = {
      name: "RootSpec",
      inputDefSet: { tags: [] },
      outputDefSet: { tags: [] },
    };
    const graph = new DefGraph({ root: rootSpec });
    const rootSpecNodeId = graph.getRootSpecNodeId();
    const rootSpecNode = graph.getNodeAttributes(rootSpecNodeId);
    expect(rootSpecNode).toHaveProperty("nodeType", "root-spec");
    expect(rootSpecNode).toHaveProperty("specName", rootSpec.name);
  });

  it("should add connected dual specs", () => {
    const graph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: [] },
        outputDefSet: { tags: [] },
      },
    });
    const from = { specName: "SpecA", output: "outputA" };
    const to = { specName: "SpecB", input: "inputB", hasTransform: false };
    graph.addConnectedDualSpecs(from, to);
    const fromSpecNodeId = graph.findNode(
      (_, attrs) =>
        attrs.nodeType === "spec" && attrs.specName === from.specName
    );
    const toSpecNodeId = graph.findNode(
      (_, attrs) => attrs.nodeType === "spec" && attrs.specName === to.specName
    );
    expect(fromSpecNodeId).not.toBeNull();
    expect(toSpecNodeId).not.toBeNull();
    w;
    const fromOutletNodeId = graph.outboundNeighbors(fromSpecNodeId!)[0];
    expect(fromOutletNodeId).not.toBeNull();
    const streamNodeId = graph.outboundNeighbors(fromOutletNodeId!)[0];
    expect(streamNodeId).not.toBeNull();
    const toInletNodeId = graph.outboundNeighbors(streamNodeId!)[0];
    expect(toInletNodeId).not.toBeNull();
    expect(graph.hasEdge(toInletNodeId!, toSpecNodeId!)).toBe(true);
  });
});
