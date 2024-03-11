use petgraph::graph::{DiGraph, NodeIndex};
use serde::{Deserialize, Serialize};
use crate::systems::def_graph::{DefGraph, DefGraphNode, NodeType};
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
    graph: DiGraph<InstantiatedGraphNode, ()>,
    node_indices: HashMap<String, NodeIndex>,
    pub context_id: String,
    pub root_job_id: String,
    pub stream_id_overrides: HashMap<String, String>,
    pub inlet_has_transform_overrides_by_tag: HashMap<String, bool>,
    pub stream_source_spec_type_by_stream_id: HashMap<String, (String, String)>,
    pub def_graph: DefGraph,
}

impl InstantiatedGraph {
    pub fn new(
        context_id: String,
        root_job_id: String,
        stream_id_overrides: HashMap<String, String>,
        inlet_has_transform_overrides_by_tag: HashMap<String, bool>,
        stream_source_spec_type_by_stream_id: HashMap<String, (String, String)>,
        def_graph: DefGraph,
    ) -> Self {
        let graph = DiGraph::<InstantiatedGraphNode, ()>::new();
        let node_indices = HashMap::new();
        InstantiatedGraph {
            graph,
            node_indices,
            context_id,
            root_job_id,
            stream_id_overrides,
            inlet_has_transform_overrides_by_tag,
            stream_source_spec_type_by_stream_id,
            def_graph,
        }
    }

    // Methods to manipulate the graph will be added here
}
