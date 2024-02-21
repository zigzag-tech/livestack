use crate::systems::def_graph_utils::{unique_spec_identifier, unique_stream_identifier};
use petgraph::graph::DiGraph;
// use petgraph::graph::Node;
use petgraph::graph::NodeIndex;
use serde::{Deserialize, Serialize};
// use napi_derive::napi;

use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum NodeType {
    RootSpec,
    Spec,
    StreamDef,
    Inlet,
    Outlet,
    Alias,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct DefGraphNode {
    pub node_type: NodeType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spec_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unique_spec_label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tag: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub has_transform: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_def_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
    pub label: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct DefGraph {
    pub graph: DiGraph<DefGraphNode, ()>,
    node_indices: HashMap<String, NodeIndex>,
}

impl DefGraph {
    /// Retrieves all node indices.
    pub fn get_all_node_indices(&self) -> Vec<NodeIndex> {
        self.node_indices.values().cloned().collect()
    }

    pub fn node_weight(&self, index: NodeIndex) -> Option<&DefGraphNode> {
        self.graph.node_weight(index)
    }

    pub fn raw_edges(&self) -> Vec<(NodeIndex, NodeIndex)> {
        self.graph.raw_edges().iter().map(|edge| (edge.source(), edge.target())).collect()
    }

    pub fn contains_edge(&self, from: NodeIndex, to: NodeIndex) -> bool {
        self.graph.contains_edge(from, to)
    }
}
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SpecBase {
    pub name: String,
    pub input_tags: Vec<String>,
    pub output_tags: Vec<String>,
}

impl DefGraph {
    /// Deserializes a `DefGraph` from a JSON string.
    pub fn load_from_json(json_str: &str) -> Result<Self, serde_json::Error> {
        // Deserialize the JSON string to a SerializedDefGraph
        let graph: DefGraph = serde_json::from_str(json_str)?;

        Ok(graph)
    }
    /// Serializes the `DefGraph` to a JSON string.
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
       
        // Serialize it to a JSON string
        serde_json::to_string(&self)
    }
    pub fn get_all_alias_node_ids(&self) -> Vec<NodeIndex> {
        self.graph
            .node_indices()
            .filter(|&index| {
                if let Some(node) = self.graph.node_weight(index) {
                    node.node_type == NodeType::Alias
                } else {
                    false
                }
            })
            .collect()
    }

    pub fn get_root_spec_node_id(&self) -> Option<NodeIndex> {
        self.graph.node_indices().find(|&index| {
            if let Some(node) = self.graph.node_weight(index) {
                node.node_type == NodeType::RootSpec
            } else {
                false
            }
        })
    }
    pub fn lookup_spec_and_tag_by_alias(
        &self,
        alias: &str,
        direction: &str,
    ) -> Option<(String, String, Option<String>)> {
        let root_spec_node_id = self.get_root_spec_node_id()?;
        let alias_node_id = match direction {
            "in" => self.find_inbound_neighbor(root_spec_node_id, |node| {
                node.node_type == NodeType::Alias && node.alias.as_deref() == Some(alias)
            }),
            "out" => self.find_outbound_neighbor(root_spec_node_id, |node| {
                node.node_type == NodeType::Alias && node.alias.as_deref() == Some(alias)
            }),
            _ => None,
        }?;

        let (spec_node_id, tag) = match direction {
            "in" => {
                let inlet_node_id = self.find_inbound_neighbor(alias_node_id, |node| {
                    node.node_type == NodeType::Inlet
                })?;
                let spec_node_id = self.find_outbound_neighbor(inlet_node_id, |node| {
                    node.node_type == NodeType::Spec
                })?;
                let tag = self.graph.node_weight(inlet_node_id)?.tag.clone()?;
                (spec_node_id, tag)
            }
            "out" => {
                let outlet_node_id = self.find_outbound_neighbor(alias_node_id, |node| {
                    node.node_type == NodeType::Outlet
                })?;
                let spec_node_id = self.find_inbound_neighbor(outlet_node_id, |node| {
                    node.node_type == NodeType::Spec
                })?;
                let tag = self.graph.node_weight(outlet_node_id)?.tag.clone()?;
                (spec_node_id, tag)
            }
            _ => return None,
        };

        let spec_name = self.graph.node_weight(spec_node_id)?.spec_name.clone()?;
        let unique_spec_label = self
            .graph
            .node_weight(spec_node_id)?
            .unique_spec_label
            .clone();

        Some((spec_name, tag, unique_spec_label))
    }

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
        condition: F,
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
        condition: F,
    ) -> Option<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.filter_outbound_neighbors(node_id, condition)
            .into_iter()
            .next()
    }

    pub fn add_connected_dual_specs(
        &mut self,
        from: (&str, &str, Option<&str>), // (spec_name, output, unique_spec_label)
        to: (&str, &str, bool, Option<&str>), // (spec_name, input, has_transform, unique_spec_label)
    ) -> (NodeIndex, NodeIndex, NodeIndex, NodeIndex, NodeIndex) {
        let from_spec_id = unique_spec_identifier(from.0, from.2);
        let from_spec_node_id = self.ensure_node(
            &from_spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(from.0.to_string()),
                unique_spec_label: from.2.map(String::from),
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: from_spec_id.clone(),
            },
        );

        let from_outlet_node_id = self.ensure_node(
            &format!("{}_{}", from_spec_id, from.1),
            DefGraphNode {
                node_type: NodeType::Outlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(from.1.to_string()),
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", from_spec_id, from.1),
            },
        );

        let to_spec_id = unique_spec_identifier(to.0, to.3);
        let to_spec_node_id = self.ensure_node(
            &to_spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(to.0.to_string()),
                unique_spec_label: to.3.map(String::from),
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: to_spec_id.clone(),
            },
        );

        let to_inlet_node_id = self.ensure_node(
            &format!("{}_{}", to_spec_id, to.1),
            DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(to.1.to_string()),
                has_transform: Some(to.2),
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}_{}", to_spec_id, to.1),
            },
        );

        let stream_def_id = unique_stream_identifier(
            Some(from.0),
            Some(from.1),
            from.2,
            Some(to.0),
            Some(to.1),
            to.3,
        );
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

        self.ensure_edge(from_spec_node_id, from_outlet_node_id);
        self.ensure_edge(from_outlet_node_id, stream_node_id);
        self.ensure_edge(stream_node_id, to_inlet_node_id);
        self.ensure_edge(to_inlet_node_id, to_spec_node_id);

        (
            from_spec_node_id,
            to_spec_node_id,
            stream_node_id,
            from_outlet_node_id,
            to_inlet_node_id,
        )
    }
    pub fn assign_alias(
        &mut self,
        alias: &str,
        spec_name: &str,
        root_spec_name: &str,
        unique_spec_label: Option<&str>,
        type_: &str,
        tag: &str,
    ) {
        let spec_node_id = self
            .find_node(|node| {
                node.node_type == NodeType::Spec
                    && node.spec_name.as_deref() == Some(spec_name)
                    && node.unique_spec_label.as_deref() == unique_spec_label
            })
            .expect("Spec node not found");

        let root_spec_node_id = self
            .find_node(|node| {
                node.node_type == NodeType::RootSpec
                    && node.spec_name.as_deref() == Some(root_spec_name)
            })
            .expect("Root spec node not found");

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
                let inlet_node_id = self
                    .find_inbound_neighbor(spec_node_id, |node| {
                        node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag)
                    })
                    .expect("Inlet node not found");
                self.ensure_edge(inlet_node_id, alias_node_id);
                self.ensure_edge(alias_node_id, root_spec_node_id);
            }
            "out" => {
                let outlet_node_id = self
                    .find_outbound_neighbor(spec_node_id, |node| {
                        node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag)
                    })
                    .expect("Outlet node not found");
                self.ensure_edge(root_spec_node_id, alias_node_id);
                self.ensure_edge(alias_node_id, outlet_node_id);
            }
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
    pub fn ensure_edge(&mut self, from_index: NodeIndex, to_index: NodeIndex) {
        if !self.graph.contains_edge(from_index, to_index) {
            self.graph.add_edge(from_index, to_index, ());
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
            graph,
            node_indices,
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
