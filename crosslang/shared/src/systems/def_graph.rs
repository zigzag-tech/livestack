use petgraph::graph::{DiGraph, NodeIndex};
use std::collections::HashMap;

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
