import { InstantiatedGraph, StreamIdOverridesForRootSpec } from "./InstantiatedGraph";
import * as DefGraphModule from "./DefGraph";

/**
 * A minimal fake defGraph to support our test.
 * It provides the methods used by InstantiatedGraph:
 *   - nodes() returns an array of node IDs.
 *   - getNodeAttributes(id) returns the node's data.
 *   - edges() returns an object with a "results" array.
 *   - outboundNeighbors() and inboundNeighbors() for stream-related queries.
 */
class FakeDefGraph {
  private _nodes: Map<string, any>;
  private _edges: Array<{ source: string; target: string }>;

  constructor() {
    this._nodes = new Map();
    this._edges = [];
  }

  // Adds a node with the given ID and attributes.
  addNode(id: string, data: any) {
    this._nodes.set(id, data);
  }

  // Convenience method matching the defGraph.ensure_node() signature.
  ensure_node(id: string, data: any): string {
    this.addNode(id, data);
    return id;
  }

  // Return all node IDs.
  nodes(): string[] {
    return Array.from(this._nodes.keys());
  }

  // Returns the node attributes by ID.
  getNodeAttributes(id: string): any {
    return this._nodes.get(id);
  }

  // Returns an object whose "results" contains the defined edges.
  edges(): { results: { source: string; target: string }[] } {
    return { results: this._edges };
  }

  // Adds an edge between the given source and target IDs.
  addEdge(source: string, target: string) {
    this._edges.push({ source, target });
  }

  // For stream connectivity queries (not used in this minimal test).
  outboundNeighbors(nodeId: string): string[] {
    return this._edges
      .filter((e) => e.source === nodeId)
      .map((e) => e.target);
  }

  inboundNeighbors(nodeId: string): string[] {
    return this._edges
      .filter((e) => e.target === nodeId)
      .map((e) => e.source);
  }
}

// For testing purposes we override the asynchronous genSpecIdentifier.
// In our InstantiatedGraph code the spec job id is computed as:
//    `[${contextId}]${await genSpecIdentifier(specName, uniqueSpecLabel)}`
jest.spyOn(DefGraphModule, "genSpecIdentifier").mockImplementation(async (name: string, uniqueLabel?: string) => {
  // Simply return the spec name so that job id becomes `[contextId]SpecA`
  return name;
});

describe("InstantiatedGraph", () => {
  it("should instantiate from a defGraph and create all nodes and edges properly", async () => {
    // Create our fake DefGraph.
    const fakeDefGraph = new FakeDefGraph();

    // Add a RootSpec node (this will be mapped to a root-job).
    fakeDefGraph.addNode("root", {
      nodeType: "RootSpec",
      specName: "RootSpec",
      label: "RootSpec"
    });

    // Add a Spec node (this will be mapped to a job node).
    fakeDefGraph.addNode("SpecA", {
      nodeType: "Spec",
      specName: "SpecA",
      uniqueSpecLabel: null,
      label: "SpecA"
    });

    // Add an edge from the RootSpec node to the Spec node.
    fakeDefGraph.addEdge("root", "SpecA");

    // Create the InstantiatedGraph passing in our fake defGraph and other needed props.
    const instantiatedGraph = new InstantiatedGraph({
      contextId: "testContext",
      defGraph: fakeDefGraph as any, // cast to any (or DefGraph) as needed
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

    // Check that the number of edges in the instantiated graph equals the number of edges in fakeDefGraph.
    const instantiatedEdgeCount = instantiatedGraph.edges().length;
    const defGraphEdgeCount = fakeDefGraph.edges().results.length;
    expect(instantiatedEdgeCount).toBe(defGraphEdgeCount);
  });
}); 