import { DefGraph } from './DefGraph';
import { expect } from 'chai';

describe('DefGraph', () => {
  it('should create a root spec node', () => {
    const rootSpec = { name: 'RootSpec', inputDefSet: { tags: [] }, outputDefSet: { tags: [] } };
    const graph = new DefGraph({ root: rootSpec });
    const rootSpecNodeId = graph.getRootSpecNodeId();
    const rootSpecNode = graph.getNodeAttributes(rootSpecNodeId);
    expect(rootSpecNode).to.have.property('nodeType', 'root-spec');
    expect(rootSpecNode).to.have.property('specName', rootSpec.name);
  });

  it('should add connected dual specs', () => {
    const graph = new DefGraph({ root: { name: 'RootSpec', inputDefSet: { tags: [] }, outputDefSet: { tags: [] } } });
    const from = { specName: 'SpecA', output: 'outputA' };
    const to = { specName: 'SpecB', input: 'inputB', hasTransform: false };
    graph.addConnectedDualSpecs(from, to);
    const fromSpecNodeId = graph.findNode((_, attrs) => attrs.nodeType === 'spec' && attrs.specName === from.specName);
    const toSpecNodeId = graph.findNode((_, attrs) => attrs.nodeType === 'spec' && attrs.specName === to.specName);
    expect(fromSpecNodeId).to.not.be.null;
    expect(toSpecNodeId).to.not.be.null;
    expect(graph.hasEdge(fromSpecNodeId, toSpecNodeId)).to.be.true;
  });

  // Additional test cases should be added here to cover more functionality
});
