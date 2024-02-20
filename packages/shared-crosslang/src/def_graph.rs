use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::visit::EdgeRef;
use std::collections::HashMap;
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum NodeType {
    Spec,
    RootSpec,
    Inlet,
    Outlet,
    StreamDef,
    Alias,
}

#[derive(Debug, Clone)]
pub struct DefGraphNode {
    pub node_type: NodeType,
    pub label: String,
    pub spec_name: String,
    pub unique_spec_label: Option<String>,
    pub tag: Option<String>,
    pub has_transform: Option<bool>,
    pub direction: Option<String>,
    // Additional fields as needed
}

impl DefGraphNode {
    pub fn new(node_type: NodeType, label: String) -> Self {
        DefGraphNode { node_type, label }
        DefGraphNode {
            node_type,
            label,
            spec_name,
            tag: None,
            has_transform: None,
            direction: None,
            unique_spec_label: None,
            stream_node_id_by_spec_identifier_type_and_tag,
        }
    }

    // Additional methods to set optional fields
    pub fn with_spec_name(mut self, spec_name: String) -> Self {
        self.spec_name = Some(spec_name);
        self
    }

    pub fn with_tag(mut self, tag: String) -> Self {
        self.tag = Some(tag);
        self
    }

    pub fn with_stream_def_id(mut self, stream_def_id: String) -> Self {
        self.stream_def_id = Some(stream_def_id);
        self
    }

    pub fn with_alias(mut self, alias: String) -> Self {
        self.alias = Some(alias);
        self
    }

    pub fn with_has_transform(mut self, has_transform: bool) -> Self {
        self.has_transform = Some(has_transform);
        self
    }

    pub fn with_direction(mut self, direction: String) -> Self {
        self.direction = Some(direction);
        self
    }

    pub fn with_unique_spec_label(mut self, unique_spec_label: String) -> Self {
        self.unique_spec_label = Some(unique_spec_label);
        self
    }
}

pub struct DefGraph {
    graph: DiGraph<DefGraphNode, ()>,
    stream_node_id_by_spec_identifier_type_and_tag: HashMap<String, NodeIndex>,
    // Additional fields as needed
}

impl DefGraph {
    pub fn new() -> Self {
        let graph = DiGraph::new();
        let stream_node_id_by_spec_identifier_type_and_tag = HashMap::new();
        DefGraph { graph }
    }

    pub fn ensure_node(&mut self, id: &str, data: DefGraphNode) -> NodeIndex {
        let node_id = format!("{}_{}", data.node_type.as_str(), id);
        let nodes = self.graph.node_indices().find(|&n| self.graph[n].label == node_id);
        match nodes {
            Some(node_index) => node_index,
            None => {
                let node_index = self.graph.add_node(data);
                self.stream_node_id_by_spec_identifier_type_and_tag.insert(node_id, node_index);
                node_index
            }
        }
    }

    // Methods to manipulate the graph (add nodes, edges, etc.)
    // TODO: Implement methods equivalent to TypeScript implementation
    // ...
}

impl NodeType {
    pub fn as_str(&self) -> &str {
        match self {
            NodeType::Spec => "spec",
            NodeType::RootSpec => "root-spec",
            NodeType::Inlet => "inlet",
            NodeType::Outlet => "outlet",
            NodeType::StreamDef => "stream-def",
            NodeType::Alias => "alias",
        }
    }
}

// Implement the methods to mimic the TypeScript functionality
// ...
