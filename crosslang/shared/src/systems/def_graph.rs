use petgraph::graph::{DiGraph, NodeIndex};
use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq)]
pub enum NodeType {
    RootSpec,
    Spec,
    StreamDef,
    Inlet,
    Outlet,
    Alias,
}

#[derive(Clone, Debug, PartialEq)]
pub struct DefGraphNode {
    pub node_type: NodeType,
    pub spec_name: Option<String>,
    pub unique_spec_label: Option<String>,
    pub tag: Option<String>,
    pub has_transform: Option<bool>,
    pub stream_def_id: Option<String>,
    pub alias: Option<String>,
    pub direction: Option<String>,
    pub label: String,
}

pub struct DefGraph {
    graph: DiGraph<DefGraphNode, ()>,
    node_indices: HashMap<String, NodeIndex>,
}

impl DefGraph {
    pub fn new() -> Self {
        DefGraph {
            graph: DiGraph::<DefGraphNode, ()>::new(),
            node_indices: HashMap::new(),
        }
    }

    pub fn get_spec_node_ids(&self) -> Vec<NodeIndex> {
        self.graph.node_indices()
            .filter(|&index| {
                if let Some(node) = self.graph.node_weight(index) {
                    node.node_type == NodeType::Spec
                } else {
                    false
                }
            })
            .collect()
    }

    pub fn ensure_node(&mut self, id: &str, data: DefGraphNode) -> NodeIndex {
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

// Additional methods to interact with the graph can be added here.

#[cfg(test)]
mod tests {
    use super::*;
    use assert_matches::assert_matches;
    use petgraph::dot::{Dot, Config};

    #[test]
    fn test_get_spec_node_ids() {
        let mut graph = DefGraph::new();

        // Add a spec node
        let spec_node_data = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("SpecA".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "SpecA".to_string(),
        };
        let spec_node_id = graph.ensure_node("SpecA", spec_node_data);

        // Add a non-spec node
        let non_spec_node_data = DefGraphNode {
            node_type: NodeType::StreamDef,
            spec_name: None,
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: Some("StreamA".to_string()),
            alias: None,
            direction: None,
            label: "StreamA".to_string(),
        };
        graph.ensure_node("StreamA", non_spec_node_data);

        // Retrieve spec node IDs
        let spec_node_ids = graph.get_spec_node_ids();
        assert!(spec_node_ids.contains(&spec_node_id));
        assert_eq!(spec_node_ids.len(), 1);
    }

    #[test]
    fn test_ensure_node() {
        let mut graph = DefGraph::new();
        // ... (rest of the test_ensure_node)

        // Ensure a node is created
        let test_node = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("TestSpec".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "TestNode".to_string(),
        };
        let node_id = graph.ensure_node("TestNode", test_node.clone());
        assert_matches!(graph.graph.node_weight(node_id), Some(node) if node == &test_node);

        // Ensure the same node is retrieved with the same ID
        let same_node_id = graph.ensure_node("TestNode", test_node.clone());
        assert_eq!(node_id, same_node_id);

        // Ensure the node data is not overwritten if the same ID is used
        // assign a different value to the `label` field
        let different_test_node = DefGraphNode {
            label: "DifferentTestNode".to_string(),
            ..test_node.clone()
        };

        let same_node_id_with_different_data =
            graph.ensure_node("TestNode", different_test_node.clone());
        assert_eq!(
            node_id, same_node_id_with_different_data,
            "The node data should not be overwritten if the same ID is used",
        );
        let node_data = graph.graph.node_weight(same_node_id_with_different_data);

        assert_matches!(
            node_data,
            Some(node) if node == &test_node,
            "The node data should not be overwritten if the same ID is used",
        );
    }
}
