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
    const fromOutletNodeId = graph.outboundNeighbors(fromSpecNodeId!)[0];
    expect(fromOutletNodeId).not.toBeNull();
    const streamNodeId = graph.outboundNeighbors(fromOutletNodeId!)[0];
    expect(streamNodeId).not.toBeNull();
    const toInletNodeId = graph.outboundNeighbors(streamNodeId!)[0];
    expect(toInletNodeId).not.toBeNull();
    expect(graph.hasEdge(toInletNodeId!, toSpecNodeId!)).toBe(true);
  });

  it("should return spec node IDs", () => {
    const graph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: [] },
        outputDefSet: { tags: [] },
      },
    });
    // Add a spec node
    const specNodeId = graph.ensureNode("SpecA", {
      nodeType: "spec",
      specName: "SpecA",
      label: "SpecA",
    });
    // Retrieve spec node IDs
    const specNodeIds = graph.getSpecNodeIds();
    expect(specNodeIds).toContain(specNodeId);
  });

  it("should ensure nodes are created and retrieved correctly", () => {
    const graph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: [] },
        outputDefSet: { tags: [] },
      },
    });
    const nodeId = graph.ensureNode("TestNode", {
      nodeType: "spec",
      specName: "TestSpec",
      label: "TestNode",
    });
    expect(nodeId).not.toBeNull();
    const retrievedNode = graph.getNodeAttributes(nodeId);
    expect(retrievedNode).toEqual({
      nodeType: "spec",
      specName: "TestSpec",
      label: "TestNode",
    });

    // Ensure the same node is retrieved when called again with the same id
    const sameNodeId = graph.ensureNode("TestNode", {
      nodeType: "spec",
      specName: "TestSpec",
      label: "TestNode",
    });
    expect(sameNodeId).toEqual(nodeId);
  });
});
