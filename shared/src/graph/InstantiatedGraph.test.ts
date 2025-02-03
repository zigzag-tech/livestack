import { InstantiatedGraph, StreamIdOverridesForRootSpec } from "./InstantiatedGraph";
import * as DefGraphModule from "./DefGraph";
import { initDefGraph } from "./DefGraph";
import { getNodesConnectedToStream } from "./InstantiatedGraph";

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
    const instantiatedGraph = new InstantiatedGraph({
      contextId: "testContext",
      defGraph: realDefGraph as any, // cast to any (or DefGraph) as needed
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });

    // Wait for asynchronous instantiation to complete.
    await instantiatedGraph.initPromise;

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
    const instantiatedEdgeCount = instantiatedGraph.edges().length;
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

    const instantiatedGraph = new InstantiatedGraph({
      contextId: "multiTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });
    await instantiatedGraph.initPromise;

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

    const instantiatedGraph = new InstantiatedGraph({
      contextId: "nodeCountTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });
    await instantiatedGraph.initPromise;

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

    const instantiatedGraph = new InstantiatedGraph({
      contextId: "attrTest",
      defGraph: realDefGraph,
      streamIdOverrides: {} as StreamIdOverridesForRootSpec,
      rootJobId: "rootJob",
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });
    await instantiatedGraph.initPromise;

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

describe("getNodesConnectedToStream", () => {
  let graph: InstantiatedGraph;

  // Create a dummy defGraph that does nothing since we will manually add nodes later.
  const dummyDefGraph: any = {
    nodes: () => [],
    edges: () => ({ results: [] }),
    getNodeAttributes: (_: number | string) => {
      // This will be reimplemented by the graph's own getNodeAttributes after we add nodes.
      return undefined;
    },
    inboundNeighbors: (id: number) => graph.inboundNeighbors(id),
    outboundNeighbors: (id: number) => graph.outboundNeighbors(id),
    hasEdge: (from: number | string, to: number | string) => graph.hasEdge(from, to),
  };

  beforeEach(async () => {
    // Create a new InstantiatedGraph with an empty defGraph.
    graph = new InstantiatedGraph({
      contextId: "test",
      defGraph: dummyDefGraph,
      rootJobId: "rootJob",
      streamIdOverrides: {},
      inletHasTransformOverridesByTag: {},
      streamSourceSpecTypeByStreamId: {},
    });
    await graph.initPromise;
  });

  it("should return null source and empty targets for an isolated stream node", () => {
    const streamId = "isolatedStream";
    graph.addNode(streamId, {
      nodeType: "stream",
      streamId,
      label: streamId,
    });
    const result = getNodesConnectedToStream(graph, streamId);
    expect(result.source).toBeNull();
    expect(result.targets).toEqual([]);
  });

  it("should return proper source for a stream with an inbound outlet connection", () => {
    // Create a connection chain: sourceSpec (job) -> outlet -> stream
    const sourceSpecId = "sourceSpec";
    graph.addNode(sourceSpecId, {
      nodeType: "job",
      jobId: "[test]sourceJob",
      specName: "SpecA",
      label: "SpecA",
    });
    const outletId = "outlet1";
    graph.addNode(outletId, {
      nodeType: "Outlet",
      tag: "out",
      label: "SpecA/out",
    });
    const streamId = "stream1";
    graph.addNode(streamId, {
      nodeType: "stream",
      streamId,
      label: streamId,
    });
    // Create the edges: sourceSpec -> outlet, then outlet -> stream.
    graph.addEdge(sourceSpecId, outletId);
    graph.addEdge(outletId, streamId);
    const result = getNodesConnectedToStream(graph, streamId);
    expect(result.source).not.toBeNull();
    expect(result.source?.origin).toMatchObject({
      nodeType: "job",
      jobId: "[test]sourceJob",
      specName: "SpecA",
      label: "SpecA",
    });
    expect(result.source?.outletNode).toMatchObject({
      nodeType: "Outlet",
      tag: "out",
      label: "SpecA/out",
    });
    expect(result.targets).toEqual([]);
  });

  it("should return proper targets for a stream with an outbound inlet connection", () => {
    // Create a chain: stream -> inlet -> destinationSpec
    const streamId = "stream2";
    graph.addNode(streamId, {
      nodeType: "stream",
      streamId,
      label: streamId,
    });
    const inletId = "inlet1";
    graph.addNode(inletId, {
      nodeType: "Inlet",
      tag: "in",
      hasTransform: false,
      label: "SpecB/in",
    });
    const destSpecId = "destSpec";
    graph.addNode(destSpecId, {
      nodeType: "job",
      jobId: "[test]destJob",
      specName: "SpecB",
      label: "SpecB",
    });
    // Create the edges: stream -> inlet, then inlet -> destination spec.
    graph.addEdge(streamId, inletId);
    graph.addEdge(inletId, destSpecId);
    const result = getNodesConnectedToStream(graph, streamId);
    expect(result.source).toBeNull();
    expect(result.targets.length).toBe(1);
    expect(result.targets[0].inletNode).toMatchObject({
      nodeType: "Inlet",
      tag: "in",
      hasTransform: false,
      label: "SpecB/in",
    });
    expect(result.targets[0].destination).toMatchObject({
      nodeType: "job",
      jobId: "[test]destJob",
      specName: "SpecB",
      label: "SpecB",
    });
  });

  it("should return both source and targets when stream node is connected on both sides", () => {
    // Create a complete chain: sourceSpec -> outlet -> stream -> inlet -> destSpec.
    const sourceSpecId = "sourceSpec2";
    graph.addNode(sourceSpecId, {
      nodeType: "job",
      jobId: "[test]sourceJob2",
      specName: "SpecA",
      label: "SpecA",
    });
    const outletId = "outlet2";
    graph.addNode(outletId, {
      nodeType: "Outlet",
      tag: "out",
      label: "SpecA/out",
    });
    const streamId = "stream3";
    graph.addNode(streamId, {
      nodeType: "stream",
      streamId,
      label: streamId,
    });
    const inletId = "inlet2";
    graph.addNode(inletId, {
      nodeType: "Inlet",
      tag: "in",
      hasTransform: false,
      label: "SpecB/in",
    });
    const destSpecId = "destSpec2";
    graph.addNode(destSpecId, {
      nodeType: "job",
      jobId: "[test]destJob2",
      specName: "SpecB",
      label: "SpecB",
    });
    // Connect source part
    graph.addEdge(sourceSpecId, outletId);
    graph.addEdge(outletId, streamId);
    // Connect target part
    graph.addEdge(streamId, inletId);
    graph.addEdge(inletId, destSpecId);
    const result = getNodesConnectedToStream(graph, streamId);
    expect(result.source).not.toBeNull();
    expect(result.source?.origin).toMatchObject({
      nodeType: "job",
      jobId: "[test]sourceJob2",
      specName: "SpecA",
      label: "SpecA",
    });
    expect(result.source?.outletNode).toMatchObject({
      nodeType: "Outlet",
      tag: "out",
      label: "SpecA/out",
    });
    expect(result.targets.length).toBe(1);
    expect(result.targets[0].inletNode).toMatchObject({
      nodeType: "Inlet",
      tag: "in",
      hasTransform: false,
      label: "SpecB/in",
    });
    expect(result.targets[0].destination).toMatchObject({
      nodeType: "job",
      jobId: "[test]destJob2",
      specName: "SpecB",
      label: "SpecB",
    });
  });

  it("should return multiple targets when the stream connects to more than one inlet", () => {
    // Create a stream that connects to two inlets.
    const streamId = "stream4";
    graph.addNode(streamId, {
      nodeType: "stream",
      streamId,
      label: streamId,
    });

    // First target branch
    const inletId1 = "inlet3";
    graph.addNode(inletId1, {
      nodeType: "Inlet",
      tag: "in1",
      hasTransform: false,
      label: "SpecC/in1",
    });
    const destSpecId1 = "destSpec3";
    graph.addNode(destSpecId1, {
      nodeType: "job",
      jobId: "[test]destJob3",
      specName: "SpecC",
      label: "SpecC",
    });
    graph.addEdge(streamId, inletId1);
    graph.addEdge(inletId1, destSpecId1);

    // Second target branch
    const inletId2 = "inlet4";
    graph.addNode(inletId2, {
      nodeType: "Inlet",
      tag: "in2",
      hasTransform: false,
      label: "SpecD/in2",
    });
    const destSpecId2 = "destSpec4";
    graph.addNode(destSpecId2, {
      nodeType: "job",
      jobId: "[test]destJob4",
      specName: "SpecD",
      label: "SpecD",
    });
    graph.addEdge(streamId, inletId2);
    graph.addEdge(inletId2, destSpecId2);

    const result = getNodesConnectedToStream(graph, streamId);
    expect(result.source).toBeNull();
    expect(result.targets.length).toBe(2);
    const targetTags = result.targets.map(t => t.inletNode.tag);
    expect(targetTags).toContain("in1");
    expect(targetTags).toContain("in2");
  });
}); 