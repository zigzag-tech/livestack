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
    pub spec_name: Option<String>,
    pub tag: Option<String>,
    pub stream_def_id: Option<String>,
    pub alias: Option<String>,
    pub unique_spec_label: Option<String>,
    // Additional fields as needed
}

impl DefGraphNode {
    pub fn new(node_type: NodeType, label: String) -> Self {
        DefGraphNode { node_type, label }
        DefGraphNode {
            node_type,
            label,
            spec_name: None,
            tag: None,
            stream_def_id: None,
            alias: None,
            unique_spec_label: None,
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

    pub fn with_unique_spec_label(mut self, unique_spec_label: String) -> Self {
        self.unique_spec_label = Some(unique_spec_label);
        self
    }
}

pub struct DefGraph {
    graph: DiGraph<DefGraphNode, ()>,
    // Additional fields as needed
}

impl DefGraph {
    pub fn new() -> Self {
        let graph = DiGraph::new();
        DefGraph { graph }
    }

    // Methods to manipulate the graph (add nodes, edges, etc.)
    // ...
}

// Implement the methods to mimic the TypeScript functionality
// ...
