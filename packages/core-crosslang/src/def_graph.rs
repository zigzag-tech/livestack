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
    // Additional fields as needed
}

impl DefGraphNode {
    pub fn new(node_type: NodeType, label: String) -> Self {
        DefGraphNode { node_type, label }
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
