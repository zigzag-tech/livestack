import { DefGraph } from "./DefGraph";
import { InstantiatedGraph } from "./InstantiatedGraph";

describe("InstantiatedGraph", () => {
  it("should instantiate from a DefGraph and create all nodes and edges properly", () => {
    const defGraph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: ["input1", "input2"] },
        outputDefSet: { tags: ["output1", "output2"] },
      },
    });

    // Add a spec node to the DefGraph
    const specNodeId = defGraph.ensureNode("SpecA", {
      nodeType: "spec",
      specName: "SpecA",
      label: "SpecA",
    });

    // Add inlet and outlet nodes to the DefGraph
    defGraph.ensureInletAndStream({
      specName: "SpecA",
      tag: "input1",
      hasTransform: false,
    });
    defGraph.ensureOutletAndStream({
      specName: "SpecA",
      tag: "output1",
    });

    // Instantiate an InstantiatedGraph from the DefGraph
    const instantiatedGraph = new InstantiatedGraph({
      contextId: "testContext",
      defGraph: defGraph,
      rootJobId: "rootJob",
      streamIdOverrides: {},
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    // Check if all nodes from the DefGraph are present in the InstantiatedGraph
    const nodes = instantiatedGraph.nodes();
    expect(nodes).toContain("rootJob");
    expect(nodes).toContain("[testContext](*)>>RootSpec/input1");
    expect(nodes).toContain("[testContext](*)>>RootSpec/input2");
    expect(nodes).toContain("[testContext]SpecA");

    // Check if all edges from the DefGraph are present in the InstantiatedGraph
    const edges = instantiatedGraph.edges();
    expect(edges).toHaveLength(defGraph.edges().length);
  });
});
  it("should find the correct stream node ID connected to a job", () => {
    const defGraph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: ["input1"] },
        outputDefSet: { tags: ["output1"] },
      },
    });

    // Add a spec node to the DefGraph
    const specNodeId = defGraph.ensureNode("SpecA", {
      nodeType: "spec",
      specName: "SpecA",
      label: "SpecA",
    });

    // Add inlet and outlet nodes to the DefGraph
    defGraph.ensureInletAndStream({
      specName: "SpecA",
      tag: "input1",
      hasTransform: false,
    });
    defGraph.ensureOutletAndStream({
      specName: "SpecA",
      tag: "output1",
    });

    // Instantiate an InstantiatedGraph from the DefGraph
    const instantiatedGraph = new InstantiatedGraph({
      contextId: "testContext",
      defGraph: defGraph,
      rootJobId: "rootJob",
      streamIdOverrides: {},
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    // Find the stream node ID connected to the job for input
    const inputStreamNodeId = instantiatedGraph.findStreamNodeIdConnectedToJob({
      jobId: "[testContext]SpecA",
      type: "in",
      tag: "input1",
    });
    expect(inputStreamNodeId).toContain("[testContext]");

    // Find the stream node ID connected to the job for output
    const outputStreamNodeId = instantiatedGraph.findStreamNodeIdConnectedToJob({
      jobId: "[testContext]SpecA",
      type: "out",
      tag: "output1",
    });
    expect(outputStreamNodeId).toContain("[testContext]");
  });
  it("should correctly serialize the InstantiatedGraph to JSON", () => {
    const defGraph = new DefGraph({
      root: {
        name: "RootSpec",
        inputDefSet: { tags: ["input1"] },
        outputDefSet: { tags: ["output1"] },
      },
    });

    // Add a spec node to the DefGraph
    defGraph.ensureNode("SpecA", {
      nodeType: "spec",
      specName: "SpecA",
      label: "SpecA",
    });

    // Add inlet and outlet nodes to the DefGraph
    defGraph.ensureInletAndStream({
      specName: "SpecA",
      tag: "input1",
      hasTransform: false,
    });
    defGraph.ensureOutletAndStream({
      specName: "SpecA",
      tag: "output1",
    });

    // Instantiate an InstantiatedGraph from the DefGraph
    const instantiatedGraph = new InstantiatedGraph({
      contextId: "testContext",
      defGraph: defGraph,
      rootJobId: "rootJob",
      streamIdOverrides: {},
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    // Serialize the InstantiatedGraph to JSON
    const json = instantiatedGraph.toJSON();

    // Check if the serialized JSON contains the necessary properties
    expect(json).toHaveProperty('contextId', 'testContext');
    expect(json).toHaveProperty('rootJobId', 'rootJob');
    expect(json).toHaveProperty('streamIdOverrides', {});
    expect(json).toHaveProperty('inletHasTransformOverridesByTag', {});
    expect(json).toHaveProperty('streamSourceSpecTypeByStreamId', {});
    expect(json).toHaveProperty('defGraph');
    expect(json.defGraph).toHaveProperty('nodes');
    expect(json.defGraph).toHaveProperty('edges');
  });
