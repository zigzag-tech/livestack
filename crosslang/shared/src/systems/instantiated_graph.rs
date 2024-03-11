use crate::systems::def_graph::{DefGraph, DefGraphNode, NodeType};
use petgraph::graph::{DiGraph, NodeIndex};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, PartialEq, Serialize, Deserialize, Clone)]
pub enum InstantiatedNodeType {
    RootJob,
    Job,
    Stream,
    Inlet,
    Outlet,
    Alias,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct InstantiatedGraphNode {
    pub node_type: InstantiatedNodeType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spec_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unique_spec_label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tag: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub has_transform: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
    pub label: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct InstantiatedGraph {
    pub graph: DiGraph<InstantiatedGraphNode, ()>,
    node_indices: HashMap<String, NodeIndex>,
    pub def_graph: DefGraph,
    pub context_id: String,
    pub root_job_id: String,
    pub stream_id_overrides: HashMap<String, String>,
    pub inlet_has_transform_overrides_by_tag: HashMap<String, bool>,
    pub stream_source_spec_type_by_stream_id: HashMap<String, (String, String)>,
}

impl InstantiatedGraph {
    pub fn new(
        context_id: String,
        root_job_id: String,
        stream_id_overrides: HashMap<String, String>,
        inlet_has_transform_overrides_by_tag: HashMap<String, bool>,
        stream_source_spec_type_by_stream_id: HashMap<String, (String, String)>,
        def_graph: &DefGraph,
    ) -> Self {
        let graph = DiGraph::<InstantiatedGraphNode, ()>::new();
        let node_indices = HashMap::new();
        let mut instantiated_graph = InstantiatedGraph {
            graph,
            node_indices,
            context_id,
            root_job_id,
            stream_id_overrides,
            inlet_has_transform_overrides_by_tag,
            stream_source_spec_type_by_stream_id,
            def_graph: def_graph.clone(),
        };
        instantiated_graph.instantiate();
        instantiated_graph
    }

    fn instantiate(&mut self) {
        let def_nodes = self
            .def_graph
            .graph
            .node_indices()
            .map(|index| {
                (
                    index,
                    self.def_graph.graph.node_weight(index).unwrap().clone(),
                )
            })
            .collect::<Vec<_>>();

        for (_, node) in def_nodes {
            let instantiated_node = match node.node_type {
                NodeType::RootSpec => InstantiatedGraphNode {
                    node_type: InstantiatedNodeType::RootJob,
                    job_id: Some(self.root_job_id.clone()),
                    spec_name: node.spec_name,
                    unique_spec_label: node.unique_spec_label,
                    stream_id: None,
                    tag: None,
                    has_transform: None,
                    alias: None,
                    direction: None,
                    label: self.root_job_id.clone(),
                },
                NodeType::Spec => {
                    let job_id = format!("[{}]{}", self.context_id, node.label);
                    InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Job,
                        job_id: Some(job_id.clone()),
                        spec_name: node.spec_name,
                        unique_spec_label: node.unique_spec_label,
                        stream_id: None,
                        tag: None,
                        has_transform: None,
                        alias: None,
                        direction: None,
                        label: job_id,
                    }
                }
                NodeType::StreamDef => {
                    let stream_id = self
                        .stream_id_overrides
                        .get(&node.label)
                        .cloned()
                        .unwrap_or_else(|| format!("[{}]{}", self.context_id, node.label));
                    InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Stream,
                        job_id: None,
                        spec_name: None,
                        unique_spec_label: None,
                        stream_id: Some(stream_id.clone()),
                        tag: None,
                        has_transform: None,
                        alias: None,
                        direction: None,
                        label: stream_id,
                    }
                }
                _ => node.clone().into(),
            };

            let instantiated_index = self.graph.add_node(instantiated_node);
            self.node_indices
                .insert(node.label.clone(), instantiated_index);
        }

        for edge in self.def_graph.graph.raw_edges() {
            let source_label = &self.def_graph.graph[edge.source()].label;
            let target_label = &self.def_graph.graph[edge.target()].label;
            let source_index = *self.node_indices.get(source_label).unwrap();
            let target_index = *self.node_indices.get(target_label).unwrap();
            self.graph.add_edge(source_index, target_index, ());
        }
    }

    // Methods to manipulate the graph will be added here
}

impl From<DefGraphNode> for InstantiatedGraphNode {
    fn from(node: DefGraphNode) -> Self {
        match node.node_type {
            NodeType::Inlet => InstantiatedGraphNode {
                node_type: InstantiatedNodeType::Inlet,
                job_id: None,
                spec_name: None,
                unique_spec_label: None,
                stream_id: None,
                tag: node.tag,
                has_transform: node.has_transform,
                alias: None,
                direction: None,
                label: node.label,
            },
            NodeType::Outlet => InstantiatedGraphNode {
                node_type: InstantiatedNodeType::Outlet,
                job_id: None,
                spec_name: None,
                unique_spec_label: None,
                stream_id: None,
                tag: node.tag,
                has_transform: None,
                alias: None,
                direction: None,
                label: node.label,
            },
            NodeType::Alias => InstantiatedGraphNode {
                node_type: InstantiatedNodeType::Alias,
                job_id: None,
                spec_name: None,
                unique_spec_label: None,
                stream_id: None,
                tag: None,
                has_transform: None,
                alias: node.alias,
                direction: node.direction,
                label: node.label,
            },
            _ => unreachable!(),
        }
    }
}
