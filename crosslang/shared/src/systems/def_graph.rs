use petgraph::graph::{DiGraph, NodeIndex};
use std::collections::HashMap;

#[cfg(test)]
mod tests {
    use super::*;
    use assert_matches::assert_matches;

    #[test]
    fn test_ensure_node() {
        let mut graph = DefGraph::<&str>::new();

        // Ensure a node is created
        let node_id = graph.ensure_node("TestNode", "TestData");
        assert_matches!(graph.graph.node_weight(node_id), Some(&"TestData"));

        // Ensure the same node is retrieved with the same ID
        let same_node_id = graph.ensure_node("TestNode", "TestData");
        assert_eq!(node_id, same_node_id);

        // Ensure the node data is not overwritten if the same ID is used
        let same_node_id_with_different_data = graph.ensure_node("TestNode", "DifferentData");
        assert_matches!(graph.graph.node_weight(same_node_id_with_different_data), Some(&"TestData"));
    }
}

pub struct DefGraph<N> {
    graph: DiGraph<N, ()>,
    node_indices: HashMap<String, NodeIndex>,
}

impl<N> DefGraph<N> {
    pub fn new() -> Self {
        DefGraph {
            graph: DiGraph::new(),
            node_indices: HashMap::new(),
        }
    }

    pub fn ensure_node(&mut self, id: &str, data: N) -> NodeIndex {
        match self.node_indices.get(id) {
            Some(&index) => index,
            None => {
                let index = self.graph.add_node(data);
                self.node_indices.insert(id.to_string(), index);
                index
            }
        }
    }
}
