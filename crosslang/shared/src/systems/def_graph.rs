use crate::systems::def_graph_utils::{unique_spec_identifier, unique_stream_identifier};
use petgraph::graph::DiGraph;
use petgraph::graph::Node;
use petgraph::graph::NodeIndex;
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

#[derive(Clone, Debug)]
pub struct SpecBase {
    pub name: String,
    pub input_tags: Vec<String>,
    pub output_tags: Vec<String>,
}

impl DefGraph {
    pub fn lookup_root_spec_alias(
        &self,
        spec_name: &str,
        tag: &str,
        direction: &str,
    ) -> Option<String> {
        let spec_node_id = self.find_node(|node| {
            node.node_type == NodeType::Spec && node.spec_name.as_deref() == Some(spec_name)
        })?;
        let alias_node_id = match direction {
            "in" => {
                let inlet_node_id = self.find_inbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag)
                })?;
                self.find_outbound_neighbor(inlet_node_id, |node| node.node_type == NodeType::Alias)
            }
            "out" => {
                let outlet_node_id = self.find_outbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag)
                })?;
                self.find_inbound_neighbor(outlet_node_id, |node| node.node_type == NodeType::Alias)
            }
            _ => return None,
        };

        alias_node_id.and_then(|id| {
            self.graph
                .node_weight(id)
                .and_then(|node| node.alias.clone())
        })
    }

    pub fn find_inbound_neighbor<F>(
        &self,
        node_id: NodeIndex,
        mut condition: F,
    ) -> Option<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.filter_inbound_neighbors(node_id, condition)
            .into_iter()
            .next()
    }

    pub fn find_outbound_neighbor<F>(
        &self,
        node_id: NodeIndex,
        mut condition: F,
    ) -> Option<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.filter_outbound_neighbors(node_id, condition)
            .into_iter()
            .next()
    }
}

impl DefGraph {
    pub fn assign_alias(
        &mut self,
        alias: &str,
        spec_name: &str,
        root_spec_name: &str,
        unique_spec_label: Option<&str>,
        type_: &str,
        tag: &str,
    ) {
        let spec_node_id = self.find_node(|node| {
            node.node_type == NodeType::Spec
                && node.spec_name.as_deref() == Some(spec_name)
                && node.unique_spec_label.as_deref() == unique_spec_label
        }).expect("Spec node not found");

        let root_spec_node_id = self.find_node(|node| {
            node.node_type == NodeType::RootSpec && node.spec_name.as_deref() == Some(root_spec_name)
        }).expect("Root spec node not found");

        let alias_id = format!("{}/{}", root_spec_name, alias);
        let alias_node_id = self.ensure_node(
            &alias_id,
            DefGraphNode {
                node_type: NodeType::Alias,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: Some(alias.to_string()),
                direction: Some(type_.to_string()),
                label: alias_id.clone(),
            },
        );

        match type_ {
            "in" => {
                let inlet_node_id = self.find_inbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag)
                }).expect("Inlet node not found");
                self.ensure_edge(&inlet_node_id, &alias_node_id);
                self.ensure_edge(&alias_node_id, &root_spec_node_id);
            },
            "out" => {
                let outlet_node_id = self.find_outbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag)
                }).expect("Outlet node not found");
                self.ensure_edge(&root_spec_node_id, &alias_node_id);
                self.ensure_edge(&alias_node_id, &outlet_node_id);
            },
            _ => panic!("Invalid direction type"),
        }
    }

    pub fn get_inbound_node_sets(&self, spec_node_id: NodeIndex) -> Vec<(NodeIndex, NodeIndex)> {
        self.graph
            .neighbors_directed(spec_node_id, petgraph::Incoming)
            .filter_map(|inlet_node_id| {
                if let Some(inlet_node) = self.graph.node_weight(inlet_node_id) {
                    if inlet_node.node_type == NodeType::Inlet {
                        let stream_node_id = self
                            .graph
                            .neighbors_directed(inlet_node_id, petgraph::Incoming)
                            .next() // Assuming there is only one stream node connected to the inlet
                            .expect("Inlet node should have an incoming stream node");
                        Some((inlet_node_id, stream_node_id))
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn get_outbound_node_sets(&self, spec_node_id: NodeIndex) -> Vec<(NodeIndex, NodeIndex)> {
        self.graph
            .neighbors_directed(spec_node_id, petgraph::Outgoing)
            .filter_map(|outlet_node_id| {
                if let Some(outlet_node) = self.graph.node_weight(outlet_node_id) {
                    if outlet_node.node_type == NodeType::Outlet {
                        let stream_node_id = self
                            .graph
                            .neighbors_directed(outlet_node_id, petgraph::Outgoing)
                            .next() // Assuming there is only one stream node connected to the outlet
                            .expect("Outlet node should have an outgoing stream node");
                        Some((outlet_node_id, stream_node_id))
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn filter_inbound_neighbors<F>(&self, index: NodeIndex, mut condition: F) -> Vec<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
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
        let spec_id = unique_spec_identifier(spec_name, None);
        let spec_node_id = self.ensure_node(
            &spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(spec_id.clone()),
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: spec_id.clone(),
            },
        );

        let inlet_node_id = self.ensure_node(
            &format!("{}_{}", spec_id, tag),
            DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.to_string()),
                has_transform: Some(has_transform),
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", spec_id, tag),
            },
        );

        let stream_def_id = format!("{}_{}_stream", spec_name, tag);
        let stream_node_id = self.ensure_node(
            &format!("{}_{}_stream", spec_id, tag),
            DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: format!("{}_{}_stream", spec_id, tag),
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
        let spec_id = unique_spec_identifier(spec_name, None);
        let spec_node_id = self.ensure_node(
            &spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(spec_id.clone()),
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: spec_id.clone(),
            },
        );

        let outlet_node_id = self.ensure_node(
            &format!("{}_{}", spec_id, tag),
            DefGraphNode {
                node_type: NodeType::Outlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.to_string()),
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", spec_id, tag),
            },
        );

        let stream_def_id = format!("{}_{}_stream", spec_name, tag);
        let stream_node_id = self.ensure_node(
            &format!("{}_{}_stream", spec_id, tag),
            DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: format!("{}_{}_stream", spec_id, tag),
            },
        );

        self.graph.add_edge(spec_node_id, outlet_node_id, ());
        self.graph.add_edge(outlet_node_id, stream_node_id, ());

        (outlet_node_id, stream_node_id)
    }

    pub fn new(root: SpecBase) -> Self {
        let mut graph = DiGraph::<DefGraphNode, ()>::new();
        let mut node_indices = HashMap::new();

        let root_spec_id = unique_spec_identifier(&root.name, None);
        let root_spec_node_id = graph.add_node(DefGraphNode {
            node_type: NodeType::RootSpec,
            spec_name: Some(root.name.clone()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: root_spec_id.clone(),
        });
        node_indices.insert(root_spec_id.clone(), root_spec_node_id);

        // Add inlet and outlet nodes, their edges, and the connected stream node and edges
        for tag in root.input_tags.iter() {
            let inlet_node_id = graph.add_node(DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.clone()),
                has_transform: Some(false), // This might be overwritten when instantiated
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}/{}", root_spec_id, tag),
            });
            let stream_def_id =
                unique_stream_identifier(None, Some(tag), None, Some(&root.name), Some(tag), None);
            let stream_node_id = graph.add_node(DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: stream_def_id.clone(),
            });
            graph.add_edge(stream_node_id, inlet_node_id, ());
            graph.add_edge(inlet_node_id, root_spec_node_id, ());
        }

        for tag in root.output_tags.iter() {
            let outlet_node_id = graph.add_node(DefGraphNode {
                node_type: NodeType::Outlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.clone()),
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}/{}", root_spec_id, tag),
            });
            let stream_def_id =
                unique_stream_identifier(Some(&root.name), Some(tag), None, None, Some(tag), None);
            let stream_node_id = graph.add_node(DefGraphNode {
                node_type: NodeType::StreamDef,
                spec_name: None,
                unique_spec_label: None,
                tag: None,
                has_transform: None,
                stream_def_id: Some(stream_def_id.clone()),
                alias: None,
                direction: None,
                label: stream_def_id.clone(),
            });
            graph.add_edge(root_spec_node_id, outlet_node_id, ());
            graph.add_edge(outlet_node_id, stream_node_id, ());
        }

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

    pub fn filter_outbound_neighbors<F>(&self, index: NodeIndex, mut condition: F) -> Vec<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.graph
            .neighbors_directed(index, petgraph::Outgoing)
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
    }
}
