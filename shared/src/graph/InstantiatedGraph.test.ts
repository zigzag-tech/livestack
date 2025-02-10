import { initInstantiatedGraph, InstantiatedGraph, StreamIdOverridesForRootSpec } from "./InstantiatedGraph";
import * as DefGraphModule from "./DefGraph";
import { initDefGraph } from "./DefGraph";

/**
 * Removed the FakeDefGraph class.
 * Originally a fake defGraph was defined here to support the test; it has been removed in favor of a real defGraph.
 */

// For testing purposes we override the asynchronous genSpecIdentifier.
// In our InstantiatedGraph code the spec job id is computed as:
//    `[${contextId}]${await genSpecIdentifier(specName, uniqueSpecLabel)}`
jest.spyOn(DefGraphModule, "genSpecIdentifier").mockImplementation(async (name: string, uniqueLabel?: string) => {
  // Simply return the spec name so that job id becomes `[testContext]SpecA`
  return name;
});

describe("InstantiatedGraph", () => {
  it("should instantiate from a defGraph and create all nodes and edges properly", async () => {
    // Create a real DefGraph.
    const realDefGraph = initDefGraph({
      root: {
        name: "RootSpec",
        inputTags: [],
        outputTags: [],
      },
    });
    
    // Add a SpecA node using ensureInletAndStream.
    // This call creates (if needed) a Spec node with specName 'SpecA'
    // along with an inlet node (using a dummy tag 'dummy') and a new stream node.
    (realDefGraph as any).ensureInletAndStream({ specName: "SpecA", tag: "dummy", uniqueSpecLabel: null }, false);

    // Create the InstantiatedGraph passing in our real defGraph and other needed properties.
    const instantiatedGraph = initInstantiatedGraph({
      contextId: "testContext",
      defGraph: realDefGraph as any, // cast to any (or DefGraph) as needed
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });


    // Retrieve all node IDs from the instantiated graph.
    const nodeIds = instantiatedGraph.nodes();

    // Verify that one node has the jobId "rootJob" (mapped from the RootSpec).
    const rootJobNodeFound = nodeIds.some((id) => {
      const node = instantiatedGraph.getNodeAttributes(id);
      return 'jobId' in node && node.jobId === "rootJob";
    });

    // Verify that one node has the jobId "[testContext]SpecA" (mapped from the Spec node).
    const specJobNodeFound = nodeIds.some((id) => {
      const node = instantiatedGraph.getNodeAttributes(id);
      return 'jobId' in node && node.jobId === "[testContext]SpecA";
    });

    expect(rootJobNodeFound).toBeTruthy();
    expect(specJobNodeFound).toBeTruthy();

    // Check that the number of edges in the instantiated graph equals the number of edges in realDefGraph.
    const instantiatedEdgeCount = instantiatedGraph.edgeCount();
    const defGraphEdgeCount = (realDefGraph).edges().results.length;
    expect(instantiatedEdgeCount).toBe(defGraphEdgeCount);
  });

  it("should instantiate with multiple spec nodes and compute correct job ids", async () => {
    // Create a real DefGraph.
    const realDefGraph = initDefGraph({
      root: {
        name: "RootSpec",
        inputTags: [],
        outputTags: [],
      },
    });
    // Add two Spec nodes: SpecA and SpecB using ensureInletAndStream.
    (realDefGraph).ensureInletAndStream({ specName: "SpecA", tag: "dummyA", uniqueSpecLabel: null }, false);
    (realDefGraph).ensureInletAndStream({ specName: "SpecB", tag: "dummyB", uniqueSpecLabel: null }, false);

    const instantiatedGraph = initInstantiatedGraph({
      contextId: "multiTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    const nodeIds = instantiatedGraph.nodes();
    const foundSpecA = nodeIds.some((id) => {
      const node = instantiatedGraph.getNodeAttributes(id);
      return 'jobId' in node && node.jobId === "[multiTest]SpecA";
    });
    const foundSpecB = nodeIds.some((id) => {
      const node = instantiatedGraph.getNodeAttributes(id);
      return 'jobId' in node && node.jobId === "[multiTest]SpecB";
    });

    expect(foundSpecA).toBeTruthy();
    expect(foundSpecB).toBeTruthy();
  });

  it("should have the same node count as the underlying defGraph", async () => {
    const realDefGraph = initDefGraph({
      root: {
        name: "RootSpec",
        inputTags: ["in1"],
        outputTags: ["out1"],
      },
    });
    (realDefGraph).ensureInletAndStream({ specName: "SpecA", tag: "dummy", uniqueSpecLabel: null }, false);

    const instantiatedGraph = initInstantiatedGraph({
      contextId: "nodeCountTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    const instantiatedNodeCount = instantiatedGraph.nodes().length;
    const defGraphNodeCount = realDefGraph.nodes().length;
    expect(instantiatedNodeCount).toBe(defGraphNodeCount);
  });

  it("should retrieve correct node attributes for all nodes", async () => {
    const realDefGraph = initDefGraph({
      root: {
        name: "RootSpec",
        inputTags: ["in1"],
        outputTags: ["out1"],
      },
    });
    (realDefGraph).ensureInletAndStream({ specName: "SpecA", tag: "dummy", uniqueSpecLabel: null }, false);

    const instantiatedGraph = initInstantiatedGraph({
      contextId: "attrTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    const nodeIds = instantiatedGraph.nodes();
    nodeIds.forEach((id) => {
      const attr = instantiatedGraph.getNodeAttributes(id);
      expect(attr).toHaveProperty("label");
      // For Spec and RootSpec nodes, check that the jobId property is defined.
      if (attr.nodeType === "job" || attr.nodeType === "root-job") {
        expect(attr).toHaveProperty("jobId");
      }
    });
  });
}); 