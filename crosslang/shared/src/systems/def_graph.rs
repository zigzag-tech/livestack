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
    pub graph: DiGraph<DefGraphNode, ()>,
    pub node_indices: HashMap<String, NodeIndex>,
}

impl DefGraph {
    pub fn filter_inbound_neighbors<F>(&self, node_id: &str, mut condition: F) -> Vec<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        if let Some(&index) = self.node_indices.get(node_id) {
            self.graph
                .neighbors_directed(index, petgraph::Incoming)
                .filter_map(|neighbor_index| {
                    self.graph.node_weight(neighbor_index).and_then(|node| {
                        if condition(node) {
                            Some(neighbor_index)
                        } else {
                            None
                        }
                    })
                })
                .collect()
        } else {
            vec![]
        }
    }
    pub fn ensure_edge(&mut self, from_id: &str, to_id: &str) {
        let from_index = self.node_indices.get(from_id);
        let to_index = self.node_indices.get(to_id);

        if let (Some(&from_index), Some(&to_index)) = (from_index, to_index) {
            if !self.graph.contains_edge(from_index, to_index) {
                self.graph.add_edge(from_index, to_index, ());
            }
        } else {
            panic!(
                "One or both nodes not found for IDs: {} -> {}",
                from_id, to_id
            );
        }
    }
    pub fn find_node<F>(&self, mut condition: F) -> Option<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.graph.node_indices().find(|&index| {
            if let Some(node) = self.graph.node_weight(index) {
                condition(node)
            } else {
                false
            }
        })
    }

    pub fn ensure_inlet_and_stream(
        &mut self,
        spec_name: &str,
        tag: &str,
        has_transform: bool,
    ) -> (NodeIndex, NodeIndex) {
        let spec_node_id = self.ensure_node(
            spec_name,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(spec_name.to_string()),
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: spec_name.to_string(),
            },
        );

        let inlet_node_id = self.ensure_node(
            &format!("{}_{}", spec_name, tag),
            DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.to_string()),
                has_transform: Some(has_transform),
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", spec_name, tag),
            },
        );

        let stream_def_id = format!("{}_{}_stream", spec_name, tag);
        let stream_node_id = self.ensure_node(
            &stream_def_id,
            DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: stream_def_id.clone(),
            },
        );

        self.graph.add_edge(stream_node_id, inlet_node_id, ());
        self.graph.add_edge(inlet_node_id, spec_node_id, ());

        (inlet_node_id, stream_node_id)
    }

    pub fn ensure_outlet_and_stream(
        &mut self,
        spec_name: &str,
        tag: &str,
    ) -> (NodeIndex, NodeIndex) {
        let spec_node_id = self.ensure_node(
            spec_name,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(spec_name.to_string()),
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: spec_name.to_string(),
            },
        );

        let outlet_node_id = self.ensure_node(
            &format!("{}_{}", spec_name, tag),
            DefGraphNode {
                node_type: NodeType::Outlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.to_string()),
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", spec_name, tag),
            },
        );

        let stream_def_id = format!("{}_{}_stream", spec_name, tag);
        let stream_node_id = self.ensure_node(
            &stream_def_id,
            DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: stream_def_id.clone(),
            },
        );

        self.graph.add_edge(spec_node_id, outlet_node_id, ());
        self.graph.add_edge(outlet_node_id, stream_node_id, ());

        (outlet_node_id, stream_node_id)
    }

    pub fn new() -> Self {
        DefGraph {
            graph: DiGraph::<DefGraphNode, ()>::new(),
            node_indices: HashMap::new(),
        }
    }

    pub fn get_spec_node_ids(&self) -> Vec<NodeIndex> {
        self.graph
            .node_indices()
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

// #[cfg(test)]
// mod tests {
//     use super::*;
//     use assert_matches::assert_matches;
//     use petgraph::dot::{Config, Dot};

//     #[test]
//     fn test_ensure_inlet_and_stream() {
//         let mut graph = DefGraph::new();
//         let spec_name = "TestSpec";
//         let tag = "inputTag";
//         let has_transform = true;

//         // Ensure inlet and stream nodes are created
//         let (inlet_node_id, stream_node_id) =
//             graph.ensure_inlet_and_stream(spec_name, tag, has_transform);

//         // Check if the spec node is created
//         let spec_node_id = graph
//             .find_node(|attrs| {
//                 attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
//             })
//             .expect("Spec node should exist");

//         // Check if the inlet node is created
//         assert_matches!(graph.graph.node_weight(inlet_node_id), Some(node) if node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag) && node.has_transform == Some(has_transform));

//         // Check if the inlet node is connected to the spec node
//         assert!(
//             graph.graph.contains_edge(inlet_node_id, spec_node_id),
//             "Inlet node should be connected to the spec node"
//         );

//         // Check if the stream node is created and connected to the inlet node
//         assert_matches!(graph.graph.node_weight(stream_node_id), Some(node) if node.node_type == NodeType::StreamDef && node.stream_def_id == Some(format!("{}_{}_stream", spec_name, tag)));
//         assert!(
//             graph.graph.contains_edge(stream_node_id, inlet_node_id),
//             "Stream node should be connected to the inlet node"
//         );
//     }

//     #[test]
//     fn test_get_spec_node_ids() {
//         let mut graph = DefGraph::new();

//         // Add a spec node
//         let spec_node_data = DefGraphNode {
//             node_type: NodeType::Spec,
//             spec_name: Some("SpecA".to_string()),
//             unique_spec_label: None,
//             tag: None,
//             has_transform: None,
//             stream_def_id: None,
//             alias: None,
//             direction: None,
//             label: "SpecA".to_string(),
//         };
//         let spec_node_id = graph.ensure_node("SpecA", spec_node_data);

//         // Add a non-spec node
//         let non_spec_node_data = DefGraphNode {
//             node_type: NodeType::StreamDef,
//             spec_name: None,
//             unique_spec_label: None,
//             tag: None,
//             has_transform: None,
//             stream_def_id: Some("StreamA".to_string()),
//             alias: None,
//             direction: None,
//             label: "StreamA".to_string(),
//         };
//         let non_spec_node_id = graph.ensure_node("StreamA", non_spec_node_data);

//         // Retrieve spec node IDs
//         let spec_node_ids = graph.get_spec_node_ids();
//         assert!(spec_node_ids.contains(&spec_node_id));
//         assert!(!spec_node_ids.contains(&non_spec_node_id));
//     }

//     #[test]
//     fn test_ensure_node() {
//         let mut graph = DefGraph::new();
//         // ... (rest of the test_ensure_node)

//         // Ensure a node is created
//         let test_node = DefGraphNode {
//             node_type: NodeType::Spec,
//             spec_name: Some("TestSpec".to_string()),
//             unique_spec_label: None,
//             tag: None,
//             has_transform: None,
//             stream_def_id: None,
//             alias: None,
//             direction: None,
//             label: "TestNode".to_string(),
//         };
//         let node_id = graph.ensure_node("TestNode", test_node.clone());
//         assert_matches!(graph.graph.node_weight(node_id), Some(node) if node == &test_node);

//         // Ensure the same node is retrieved with the same ID
//         let same_node_id = graph.ensure_node("TestNode", test_node.clone());
//         assert_eq!(node_id, same_node_id);

//         // Ensure the node data is not overwritten if the same ID is used
//         // assign a different value to the `label` field
//         let different_test_node = DefGraphNode {
//             label: "DifferentTestNode".to_string(),
//             ..test_node.clone()
//         };

//         let same_node_id_with_different_data =
//             graph.ensure_node("TestNode", different_test_node.clone());
//         assert_eq!(
//             node_id, same_node_id_with_different_data,
//             "The node data should not be overwritten if the same ID is used",
//         );
//         let node_data = graph.graph.node_weight(same_node_id_with_different_data);

//         assert_matches!(
//             node_data,
//             Some(node) if node == &test_node,
//             "The node data should not be overwritten if the same ID is used",
//         );
//     }

//     #[test]
//     fn test_ensure_outlet_and_stream() {
//         let mut graph = DefGraph::new();
//         let spec_name = "TestSpec";
//         let tag = "outputTag";

//         // Ensure outlet and stream nodes are created
//         let (outlet_node_id, stream_node_id) = graph.ensure_outlet_and_stream(spec_name, tag);

//         // Check if the spec node is created
//         let spec_node_id = graph
//             .find_node(|attrs| {
//                 attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
//             })
//             .expect("Spec node should exist");

//         // Check if the outlet node is created
//         assert_matches!(graph.graph.node_weight(outlet_node_id), Some(node) if node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag));

//         // Check if the outlet node is connected to the spec node
//         assert!(
//             graph.graph.contains_edge(spec_node_id, outlet_node_id),
//             "Outlet node should be connected to the spec node"
//         );

//         // Check if the stream node is created and connected to the outlet node
//         assert_matches!(graph.graph.node_weight(stream_node_id), Some(node) if node.node_type == NodeType::StreamDef && node.stream_def_id == Some(format!("{}_{}_stream", spec_name, tag)));
//         assert!(
//             graph.graph.contains_edge(outlet_node_id, stream_node_id),
//             "Stream node should be connected to the outlet node"
//         );
//     }

//     #[test]
//     fn test_ensure_edge() {
//         let mut graph = DefGraph::new();
//         let from_node = DefGraphNode {
//             node_type: NodeType::Spec,
//             spec_name: Some("FromSpec".to_string()),
//             unique_spec_label: None,
//             tag: None,
//             has_transform: None,
//             stream_def_id: None,
//             alias: None,
//             direction: None,
//             label: "FromNode".to_string(),
//         };
//         let to_node = DefGraphNode {
//             node_type: NodeType::Spec,
//             spec_name: Some("ToSpec".to_string()),
//             unique_spec_label: None,
//             tag: None,
//             has_transform: None,
//             stream_def_id: None,
//             alias: None,
//             direction: None,
//             label: "ToNode".to_string(),
//         };
//         let from_index = graph.ensure_node("FromNode", from_node);
//         let to_index = graph.ensure_node("ToNode", to_node);
//         graph.ensure_edge("FromNode", "ToNode");
//         assert!(
//             graph.graph.contains_edge(from_index, to_index),
//             "Edge should exist from FromNode to ToNode"
//         );
//         graph.ensure_edge("FromNode", "ToNode");
//         let edges: Vec<_> = graph.graph.edges_connecting(from_index, to_index).collect();
//         assert_eq!(
//             edges.len(),
//             1,
//             "There should only be one edge from FromNode to ToNode"
//         );
//     }
//     fn test_filter_inbound_neighbors() {
//         let mut graph = DefGraph::new();
//         let spec_name = "TestSpec";
//         let tag = "inputTag";
//         let has_transform = true;
//         // Ensure inlet and stream nodes are created
//         let (inlet_node_id, stream_node_id) =
//             graph.ensure_inlet_and_stream(spec_name, tag, has_transform);
//         // Add another inlet node with a different tag
//         let other_tag = "otherInputTag";
//         let (_, other_stream_node_id) =
//             graph.ensure_inlet_and_stream(spec_name, other_tag, has_transform);
//         // Filter inbound neighbors for the spec node with a specific tag
//         let filtered_inbound_neighbors = graph.filter_inbound_neighbors(spec_name, |node| {
//             node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag)
//         });
//         assert_eq!(filtered_inbound_neighbors.len(), 1);
//         assert_eq!(filtered_inbound_neighbors[0], inlet_node_id);
//         // Ensure that the other inlet node is not included
//         assert!(!filtered_inbound_neighbors.contains(&other_stream_node_id));
//     }
// }
