use crate::systems::def_graph_utils::{unique_spec_identifier, unique_stream_identifier};
use petgraph::graph::DiGraph;
// use petgraph::graph::Node;
// use napi_derive::napi;
use petgraph::graph::NodeIndex;
use serde::{Deserialize, Serialize};

use std::collections::HashMap;
use std::ops::Deref;

use super::def_graph_utils::{FromSpecAndTag, SpecTagInfo, ToSpecAndTag};

#[derive(Debug, PartialEq, Serialize, Deserialize, Clone)]
// #[napi]
pub enum NodeType {
    RootSpec,
    Spec,
    StreamDef,
    Inlet,
    Outlet,
    Alias,
}

fn node_type_to_string(node_type: &NodeType) -> String {
    match node_type {
        NodeType::RootSpec => "RootSpec".to_string(),
        NodeType::Spec => "Spec".to_string(),
        NodeType::StreamDef => "StreamDef".to_string(),
        NodeType::Inlet => "Inlet".to_string(),
        NodeType::Outlet => "Outlet".to_string(),
        NodeType::Alias => "Alias".to_string(),
    }
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

#[derive(Serialize, Deserialize, Debug, Clone)]
// #[napi]
pub struct DefGraph {
    graph: DiGraph<DefGraphNode, ()>,
    node_indices: HashMap<String, NodeIndex>,
    stream_node_id_by_spec_identifier_type_and_tag: HashMap<String, String>,
}

pub fn load_from_json(json_str: String) -> DefGraph {
    // Deserialize the JSON string to a SerializedDefGraph
    let graph: DefGraph = serde_json::from_str(&json_str)
        .expect("Failed to deserialize the JSON string to a DefGraph");

    graph
}

// #[napi]
impl DefGraph {
    /// Deserializes a `DefGraph` from a JSON string.
    // #[napi(factory)]
   
    /// Serializes the `DefGraph` to a JSON string.
    // #[napi]
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(&self)
    }

    /// Retrieves all node indices.
    pub fn get_all_node_indices(&self) -> Vec<NodeIndex> {
        self.node_indices.values().cloned().collect()
    }

    pub fn node_weight(&self, index: u32) -> Option<DefGraphNode> {
        let index = NodeIndex::new(index as usize);
        let w = self.graph.node_weight(index);
        if let Some(w) = w {
            Some(DefGraphNode {
                node_type: w.node_type.clone(),
                spec_name: w.spec_name.clone(),
                unique_spec_label: w.unique_spec_label.clone(),
                tag: w.tag.clone(),
                has_transform: w.has_transform.clone(),
                stream_def_id: w.stream_def_id.clone(),
                alias: w.alias.clone(),
                direction: w.direction.clone(),
                label: w.label.clone(),
            })
        } else {
            None
        }
    }

    pub fn raw_edges(&self) -> Vec<(u32, u32)> {
        self.graph
            .raw_edges()
            .iter()
            .map(|edge| (edge.source().index() as u32, edge.target().index() as u32))
            .collect()
    }

    pub fn contains_edge(&self, from: u32, to: u32) -> bool {
        let from = NodeIndex::new(from as usize);
        let to = NodeIndex::new(to as usize);
        self.graph.contains_edge(from, to)
    }

    pub fn node_count(&self) -> usize {
        self.graph.node_count()
    }

    pub fn edge_count(&self) -> usize {
        self.graph.edge_count()
    }

    pub fn node_indices(&self) -> Vec<u32> {
        self.graph
            .node_indices()
            .map(|index| index.index() as u32)
            .collect()
    }

    // #[napi]
    pub fn get_all_alias_node_ids(&self) -> Vec<u32> {
        self.graph
            .node_indices()
            .filter_map(|index| {
                if let Some(node) = self.graph.node_weight(index) {
                    if node.node_type == NodeType::Alias {
                        // Cast usize to u32 here
                        Some(index.index() as u32)
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn get_root_spec_node_id(&self) -> Option<u32> {
        self.graph
            .node_indices()
            .find(|&index| {
                if let Some(node) = self.graph.node_weight(index) {
                    node.node_type == NodeType::RootSpec
                } else {
                    false
                }
            })
            .map(|index| index.index() as u32) // Convert NodeIndex to u32
    }

    // #[napi]
    pub fn lookup_spec_and_tag_by_alias(
        &self,
        alias: String,
        direction: &str,
    ) -> Option<SpecTagInfo> {
        let root_spec_node_id = self.get_root_spec_node_id()?;
        let alias_node_id = match direction {
            "in" => self.find_inbound_neighbor(root_spec_node_id, |node| {
                node.node_type == NodeType::Alias && node.alias.as_deref() == Some(&alias)
            }),
            "out" => self.find_outbound_neighbor(root_spec_node_id, |node| {
                node.node_type == NodeType::Alias && node.alias.as_deref() == Some(&alias)
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
                let inlet_node_id_idx = NodeIndex::new(inlet_node_id as usize);
                let tag = self.graph.node_weight(inlet_node_id_idx)?.tag.clone()?;
                (spec_node_id, tag)
            }
            "out" => {
                let outlet_node_id = self.find_outbound_neighbor(alias_node_id, |node| {
                    node.node_type == NodeType::Outlet
                })?;
                let outlet_node_id_idx = NodeIndex::new(outlet_node_id as usize);
                let spec_node_id = self.find_inbound_neighbor(outlet_node_id, |node| {
                    node.node_type == NodeType::Spec
                })?;
                let tag = self.graph.node_weight(outlet_node_id_idx)?.tag.clone()?;
                (spec_node_id, tag)
            }
            _ => return None,
        };
        let spec_node_id_idx = NodeIndex::new(spec_node_id as usize);
        let spec_name = self
            .graph
            .node_weight(spec_node_id_idx)?
            .spec_name
            .clone()?;
        let unique_spec_label = self
            .graph
            .node_weight(spec_node_id_idx)?
            .unique_spec_label
            .clone();

        Some(SpecTagInfo {
            spec_name,
            tag,
            unique_spec_label,
        })
    }

    // #[napi]
    pub fn lookup_root_spec_alias(
        &self,
        spec_name: String,
        unique_spec_label: Option<String>,
        tag: String,
        direction: String,
    ) -> Option<String> {
        let spec_node_id = self.find_node(|node| {
            node.node_type == NodeType::Spec
                && node.spec_name.as_deref() == Some(spec_name.as_str())
                && node.unique_spec_label.as_deref() == unique_spec_label.as_deref()
        })?;

        let alias_node_id = match direction.as_str() {
            "in" => {
                let inlet_node_id = self.find_inbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Inlet
                        && node.tag.as_deref() == Some(tag.as_str())
                })?;
                self.find_outbound_neighbor(inlet_node_id, |node| {
                    node.node_type == NodeType::Alias
                })
            }
            "out" => {
                let outlet_node_id = self.find_outbound_neighbor(spec_node_id, |node| {
                    node.node_type == NodeType::Outlet
                        && node.tag.as_deref() == Some(tag.as_str())
                })?;
                self.find_inbound_neighbor(outlet_node_id, |node| {
                    node.node_type == NodeType::Alias
                })
            }
            _ => return None,
        };

        alias_node_id.and_then(|id| {
            let id_idx = NodeIndex::new(id as usize);
            self.graph
                .node_weight(id_idx)
                .and_then(|node| node.alias.clone())
        })
    }

    pub fn find_inbound_neighbor<F>(&self, node_id: u32, condition: F) -> Option<u32>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.filter_inbound_neighbors(node_id, condition)
            .into_iter()
            .next()
            .map(|index| index.index() as u32)
    }

    pub fn find_outbound_neighbor<F>(&self, node_id: u32, condition: F) -> Option<u32>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        // let node_id_index = NodeIndex::new(node_id as usize);
        self.filter_outbound_neighbors(node_id, condition)
            .into_iter()
            .next()
            .map(|index| index)
    }

    pub fn add_connected_dual_specs(
        &mut self,
        from: &FromSpecAndTag, // (spec_name, output, unique_spec_label)
        to: &ToSpecAndTag,     // (spec_name, input, has_transform, unique_spec_label)
    ) -> (u32, u32, u32, u32, u32) {
        let from_spec_id =
        unique_spec_identifier(from.spec_name.clone(), from.unique_spec_label.clone());
        let from_spec_node_id = self.ensure_node(
            &from_spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(from.spec_name.clone()),
                unique_spec_label: from.unique_spec_label.clone(),
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: from_spec_id.clone(),
            },
        );
        let from_outlet_node_id = self.ensure_node(
            &format!("{}/{}", from_spec_id, from.output),
            DefGraphNode {
                node_type: NodeType::Outlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(from.output.clone()),
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}/{}", from_spec_id, from.output),
            },
        );

        
        let stream_node_id: u32;
        let stream_def_id: String;
        let existing_stream_def_ids = self.outbound_neighbors(from_outlet_node_id);
        if existing_stream_def_ids.len()  > 1 {
            // throw error
            panic!("More than one stream def node found for outlet node");
        } else if existing_stream_def_ids.len() == 1 {
            stream_node_id = existing_stream_def_ids[0];
            stream_def_id = self.graph.node_weight(NodeIndex::new(stream_node_id as usize))
                .expect("StreamDef node must exist")
                .stream_def_id
                .clone()
                .expect("StreamDef node must have a stream_def_id");
        } else {
            stream_def_id = unique_stream_identifier(
                Some(SpecTagInfo {
                    spec_name: from.spec_name.clone(),
                    unique_spec_label: from.unique_spec_label.clone(),
                    tag: from.output.clone(),
                }),
                Some(SpecTagInfo {
                    spec_name: to.spec_name.clone(),
                    unique_spec_label: to.unique_spec_label.clone(),
                    tag: to.input.clone(),
                }),
            );
            stream_node_id = self.ensure_node(
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
        }
        let to_spec_id: String = unique_spec_identifier(to.spec_name.clone(), to.unique_spec_label.clone());

        let to_inlet_node_id = self.ensure_node(
            &format!("{}/{}", to_spec_id, to.input),
            DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(to.input.clone()),
                has_transform: Some(to.has_transform),
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}/{}", to_spec_id, to.input),
            },
        );

        let to_spec_node_id = self.ensure_node(
            &to_spec_id,
            DefGraphNode {
                node_type: NodeType::Spec,
                spec_name: Some(to.spec_name.clone()),
                unique_spec_label: to.unique_spec_label.clone(),
                tag: None,
                has_transform: None,
                stream_def_id: None,
                alias: None,
                direction: None,
                label: to_spec_id.clone(),
            },
        );

     

        self.ensure_edge(stream_node_id, to_inlet_node_id);
        self.ensure_edge(to_inlet_node_id, to_spec_node_id);

        // insert to hash
        self.stream_node_id_by_spec_identifier_type_and_tag.insert(
            format!("{}::in/{}", to.spec_name, to.input),
            stream_def_id.clone()
        );
        self.stream_node_id_by_spec_identifier_type_and_tag.insert(
            format!("{}::out/{}", from.spec_name, from.output),
            stream_def_id.clone()
        );

    
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
            .expect(format!("Spec node not found: {}", spec_name).as_str());

        let root_spec_node_id = self
            .find_node(|node| {
                node.node_type == NodeType::RootSpec
                    && node.spec_name.as_deref() == Some(root_spec_name)
            })
            .expect(format!("Root spec node not found: {}", root_spec_name).as_str());

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
                    .expect(format!("Inlet node not found: {}", tag).as_str());
                self.ensure_edge(inlet_node_id, alias_node_id);
                self.ensure_edge(alias_node_id, root_spec_node_id);
            }
            "out" => {
                let outlet_node_id = self
                    .find_outbound_neighbor(spec_node_id, |node| {
                        node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag)
                    });
                // propagate error if outlet_node_id is None
                let outlet_node_id = outlet_node_id.expect(format ! ("Outlet node not found: {}", tag).as_str());
                self.ensure_edge(root_spec_node_id, alias_node_id);
                self.ensure_edge(alias_node_id, outlet_node_id);
            }
            _ => panic!("Invalid direction type"),
        };
    }

    pub fn get_inbound_node_sets(&self, spec_node_id: u32) -> Vec<(u32, u32)> {
        let spec_node_id = NodeIndex::new(spec_node_id as usize);
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
                        let inlet_node_id = inlet_node_id.index() as u32;
                        let stream_node_id = stream_node_id.index() as u32;
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

    pub fn get_outbound_node_sets(&self, spec_node_id: u32) -> Vec<(u32, u32)> {
        let spec_node_id = NodeIndex::new(spec_node_id as usize);
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
                        let outlet_node_id = outlet_node_id.index() as u32;
                        let stream_node_id = stream_node_id.index() as u32;
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

    pub fn filter_inbound_neighbors<F>(&self, index: u32, mut condition: F) -> Vec<NodeIndex>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        let index = NodeIndex::new(index as usize);
        self.graph
            .neighbors_directed(index, petgraph::Incoming)
            .filter_map(|neighbor_index| {
                self.graph.node_weight(neighbor_index).and_then(|node| {
                    if condition(&DefGraphNode {
                        node_type: node.node_type.clone(),
                        spec_name: node.spec_name.clone(),
                        unique_spec_label: node.unique_spec_label.clone(),
                        tag: node.tag.clone(),
                        has_transform: node.has_transform.clone(),
                        stream_def_id: node.stream_def_id.clone(),
                        alias: node.alias.clone(),
                        direction: node.direction.clone(),
                        label: node.label.clone(),
                    })
                    {
                        Some(neighbor_index)
                    } else {
                        None
                    }
                })
            })
            .collect()
    }
    pub fn ensure_edge(&mut self, from_index: u32, to_index: u32) {
        let from_index = NodeIndex::new(from_index as usize);
        let to_index = NodeIndex::new(to_index as usize);
        if !self.graph.contains_edge(from_index, to_index) {
            self.graph.add_edge(from_index, to_index, ());
        }
    }
    pub fn find_node<F>(&self, mut condition: F) -> Option<u32>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        self.graph
            .node_indices()
            .find(|&index| {
                if let Some(node) = self.graph.node_weight(index) {
                    condition(&DefGraphNode {
                        node_type: node.node_type.clone(),
                        spec_name: node.spec_name.clone(),
                        unique_spec_label: node.unique_spec_label.clone(),
                        tag: node.tag.clone(),
                        has_transform: node.has_transform.clone(),
                        stream_def_id: node.stream_def_id.clone(),
                        alias: node.alias.clone(),
                        direction: node.direction.clone(),
                        label: node.label.clone(),
                    })
                } else {
                    false
                }
            })
            .map(|index| index.index() as u32)
    }

    pub fn ensure_inlet_and_stream(
        &mut self,
        s: SpecTagInfo,
        has_transform: bool,
    ) -> (u32, u32) {
        let spec_name = s.spec_name;
        let tag = s.tag;
        let spec_id = unique_spec_identifier(spec_name.to_string(), None);
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
            &format!("{}/{}", spec_id, tag),
            DefGraphNode {
                node_type: NodeType::Inlet,
                spec_name: None,
                unique_spec_label: None,
                tag: Some(tag.to_string()),
                has_transform: Some(has_transform),
                stream_def_id: None,
                alias: None,
                direction: None,
                label: format!("{}/{}", spec_id, tag),
            },
        );

        // let stream_def_id = unique_stream_identifier(None, Some(SpecTagInfo {
        //     spec_name: spec_name.clone(),
        //     tag: tag.clone(),
        //     unique_spec_label: None,
        // }));

        // check if stream_def_id exists in hash; if not, initialize
        let stream_def_id = match self.stream_node_id_by_spec_identifier_type_and_tag.get(
            format!("{}::in/{}", spec_name, tag).as_str()) {
            Some(stream_def_id) => {
                stream_def_id.clone()
            },
            None => {
                let stream_def_id = unique_stream_identifier(
                    None,
                    Some(SpecTagInfo {
                        spec_name: spec_name.clone(),
                        tag: tag.clone(),
                        unique_spec_label: None,
                    }),
                );
                let stream_def_id0 = stream_def_id.clone();
                // self.stream_node_id_by_spec_identifier_type_and_tag.insert(
                //     format!("{}::in/{}", spec_name, tag),
                //     stream_def_id.clone()
                // );
                stream_def_id0
            }
        };


        let stream_node_id = self.ensure_node(
            stream_def_id.as_str(),
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
        let stream_node_id_index = NodeIndex::new(stream_node_id as usize);
        let inlet_node_id_index = NodeIndex::new(inlet_node_id as usize);
        let spec_node_id_index = NodeIndex::new(spec_node_id as usize);

        self.graph
            .add_edge(stream_node_id_index, inlet_node_id_index, ());
        self.graph
            .add_edge(inlet_node_id_index, spec_node_id_index, ());

        (inlet_node_id, stream_node_id)
    }

    pub fn ensure_outlet_and_stream(&mut self, s: SpecTagInfo) -> (u32, u32) {
        let spec_name = s.spec_name;
        let tag = s.tag;
        let spec_name0: String = spec_name.clone();
        
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
            &format!("{}/{}", spec_id, tag),
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
        
        // let stream_def_id = unique_stream_identifier(Some(SpecTagInfo {
        //     spec_name: spec_name0.clone(),
        //     tag: tag.clone(),
        //     unique_spec_label: None,
        // }), None);

        // check if stream_def_id exists in hash; if not, initialize
        let stream_def_id = match self.stream_node_id_by_spec_identifier_type_and_tag.get(
            format!("{}::out/{}", spec_name0, tag).as_str()) {
            Some(stream_def_id) => {
                stream_def_id.clone()
            },
            None => {
                let stream_def_id = unique_stream_identifier(
                    Some(SpecTagInfo {
                        spec_name: spec_name0.clone(),
                        tag: tag.clone(),
                        unique_spec_label: None,
                    }),
                    None,
                );
                let stream_def_id0 = stream_def_id.clone();
                // self.stream_node_id_by_spec_identifier_type_and_tag.insert(
                //     format!("{}::out/{}", spec_name0, tag),
                //     stream_def_id.clone()
                // );
                stream_def_id0
            }
        };

        let stream_node_id = self.ensure_node(
            stream_def_id.as_str(),
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
        let spec_node_id_index = NodeIndex::new(spec_node_id as usize);
        let outlet_node_id_index = NodeIndex::new(outlet_node_id as usize);
        let stream_node_id_index = NodeIndex::new(stream_node_id as usize);
        self.graph
            .add_edge(spec_node_id_index, outlet_node_id_index, ());
        self.graph
            .add_edge(outlet_node_id_index, stream_node_id_index, ());

        (outlet_node_id, stream_node_id)
    }

    // #[napi(constructor)]
    pub fn new(root_spec_name: String, input_tags: Vec<String>, output_tags: Vec<String>) -> Self {
        let root_spec_name_c0: String = root_spec_name.clone();
        let root_spec_name_c1 = root_spec_name.clone();
        let mut graph = DiGraph::<DefGraphNode, ()>::new();
        let mut node_indices = HashMap::new();
        let root_spec_id = unique_spec_identifier(root_spec_name, None);
        let root_spec_node_id = graph.add_node(DefGraphNode {
            node_type: NodeType::RootSpec,
            spec_name: Some(root_spec_name_c0),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: root_spec_id.clone(),
        });
        node_indices.insert(root_spec_id.clone(), root_spec_node_id);
        let mut stream_node_id_by_spec_identifier_type_and_tag: HashMap<String, String> = HashMap::new();

        // Add inlet and outlet nodes, their edges, and the connected stream node and edges
        for tag in input_tags.iter() {
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

            // check hash first; if not found, initialize
            let stream_def_id = stream_node_id_by_spec_identifier_type_and_tag.get(
            format!("{}::in/{}", root_spec_name_c1, tag).as_str());

            let stream_def_id = match stream_def_id {
                Some(stream_def_id) => {
                stream_def_id.clone()
                },
                None => {
                    let stream_def_id = unique_stream_identifier(
                        Some(SpecTagInfo {
                            spec_name: root_spec_name_c1.clone(),
                            tag: tag.clone(),
                            unique_spec_label: None,
                        }),
                        None,
                    );
                   
                    let stream_def_id0 = stream_def_id.clone();
                    // stream_node_id_by_spec_identifier_type_and_tag.insert(
                    //     format!("{}/{}", root_spec_name_c1, tag),
                    //     stream_def_id.clone()
                    // );
                    stream_def_id0
                }
            };
            
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

        for tag in output_tags.iter() {
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
           
            // check hash first; if not found, initialize
            let stream_def_id = stream_node_id_by_spec_identifier_type_and_tag.get(
            format!("{}::out/{}", root_spec_name_c1, tag).as_str());

            let stream_def_id = match stream_def_id {
                Some(stream_def_id) => {
                stream_def_id.clone()
                },
                None => {
                    let stream_def_id = unique_stream_identifier(
                        Some(SpecTagInfo {
                            spec_name: root_spec_name_c1.clone(),
                            tag: tag.clone(),
                            unique_spec_label: None,
                        }),
                        None,
                    );
                    let stream_def_id0 = stream_def_id.clone();
                    stream_node_id_by_spec_identifier_type_and_tag.insert(
                        format!("{}/{}", root_spec_name_c1, tag),
                        stream_def_id.clone()
                    );
                    stream_def_id0
                }
            };


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

        Self {
            graph,
            node_indices,
            stream_node_id_by_spec_identifier_type_and_tag
        }
    }

    pub fn get_spec_node_ids(&self) -> Vec<u32> {
        self.graph
            .node_indices()
            .filter_map(|index| {
                if let Some(node) = self.graph.node_weight(index) {
                    if node.node_type == NodeType::Spec {
                        Some(index.index() as u32)
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn ensure_node(&mut self, id: &str, data: DefGraphNode) -> u32 {
        let full_node_id = format!("{}_{}", node_type_to_string(&data.node_type), id);
        match self.node_indices.get(&full_node_id) {
            Some(&index) => index.index() as u32,
            None => {
                let index = self.graph.add_node(DefGraphNode {
                    node_type: data.node_type,
                    spec_name: data.spec_name.clone(),
                    unique_spec_label: data.unique_spec_label.clone(),
                    tag: data.tag.clone(),
                    has_transform: data.has_transform,
                    stream_def_id: data.stream_def_id.clone(),
                    alias: data.alias.clone(),
                    direction: data.direction.clone(),
                    label: data.label.clone(),
                });
                self.node_indices.insert(full_node_id.to_string(), index);
                index.index() as u32
            }
        }
    }

    pub fn filter_outbound_neighbors<F>(&self, index: u32, mut condition: F) -> Vec<u32>
    where
        F: FnMut(&DefGraphNode) -> bool,
    {
        let index = NodeIndex::new(index as usize);
        self.graph
            .neighbors_directed(index, petgraph::Outgoing)
            .filter_map(|neighbor_index| {
                self.graph.node_weight(neighbor_index).and_then(|node| {
                    if condition(&DefGraphNode {
                        node_type: node.node_type.clone(),
                        spec_name: node.spec_name.clone(),
                        unique_spec_label: node.unique_spec_label.clone(),
                        tag: node.tag.clone(),
                        has_transform: node.has_transform.clone(),
                        stream_def_id: node.stream_def_id.clone(),
                        alias: node.alias.clone(),
                        direction: node.direction.clone(),
                        label: node.label.clone(),
                    })
                    {
                        Some(neighbor_index.index() as u32)
                    } else {
                        None
                    }
                })
            })
            .collect()
    }

    pub fn inbound_neighbors(&self, node_id: u32) -> Vec<u32> {
        let node_id = NodeIndex::new(node_id as usize);
        self.graph
            .neighbors_directed(node_id, petgraph::Incoming)
            .map(|index| index.index() as u32)
            .collect()
    }

    pub fn outbound_neighbors(&self, node_id: u32) -> Vec<u32> {
        let node_id = NodeIndex::new(node_id as usize);
        self.graph
            .neighbors_directed(node_id, petgraph::Outgoing)
            .map(|index| index.index() as u32)
            .collect()
    }
}
