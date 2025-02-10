#![allow(non_snake_case)]

use js_sys::Number;
use wasm_bindgen::prelude::*;
use tsify::Tsify;
use serde::{Deserialize, Serialize};
use serde_wasm_bindgen::{from_value, to_value};

use std::collections::HashMap;

// Import the Rust definitions from your shared crate...
use livestack_shared::systems::def_graph::load_from_json as load_def_graph_from_json_impl;
use livestack_shared::systems::instantiated_graph::{
    InstantiatedGraph as InstantiatedGraphImpl, 
    InstantiatedGraphNode as InstantiatedGraphNodeImpl, 
    InstantiatedNodeType as InstantiatedNodeTypeImpl, 
    StreamSourceSpecType, 
};
// (Optional) if you have a panic hook for better debugging:
use crate::utils::set_panic_hook;  

/// A TS-friendly version of `InstantiatedNodeType`.
#[derive(Tsify, Serialize, Deserialize, Debug, Clone)]
#[serde(rename_all(serialize = "kebab-case", deserialize = "kebab-case"))]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub enum InstantiatedNodeType {
    RootJob,
    Job,
    Stream,
    Inlet,
    Outlet,
    Alias,
}

/// A TS-friendly variant of InstantiatedGraphNode for getNodeAttributes(), etc.
#[derive(Tsify, Serialize, Deserialize, Debug, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
#[serde(rename_all = "camelCase")]
pub struct InstantiatedGraphNodeWasm {
    pub node_type: InstantiatedNodeType,
    pub job_id: Option<String>,
    pub spec_name: Option<String>,
    pub unique_spec_label: Option<String>,
    pub stream_id: Option<String>,
    pub tag: Option<String>,
    pub has_transform: Option<bool>,
    pub alias: Option<String>,
    pub direction: Option<String>,
    pub label: String,
}

/// Structures for stream "source" and "target" so they can cross the WASM boundary.
#[derive(Tsify, Serialize, Deserialize, Debug, Clone)]
#[serde(rename_all = "camelCase")]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct InstantiatedStreamConnectionSourceWasm {
    pub origin: InstantiatedGraphNodeWasm,
    pub outlet_node: InstantiatedGraphNodeWasm,
}

#[derive(Tsify, Serialize, Deserialize, Debug, Clone)]
#[serde(rename_all = "camelCase")]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct InstantiatedStreamConnectionTargetWasm {
    pub inlet_node: InstantiatedGraphNodeWasm,
    pub destination: InstantiatedGraphNodeWasm,
}

#[derive(Tsify, Serialize, Deserialize, Debug, Clone)]
#[serde(rename_all = "camelCase")]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct InstantiatedStreamConnectionsWasm {
    pub source: Option<InstantiatedStreamConnectionSourceWasm>,
    pub targets: Vec<InstantiatedStreamConnectionTargetWasm>,
}

/// The main struct bridging Rust's InstantiatedGraphImpl to JS/Wasm.  
/// Instead of receiving a DefGraph, it takes a serialized JSON string for the DefGraph.
#[wasm_bindgen(js_name = InstantiatedGraph)]
pub struct InstantiatedGraphWasm {
    inst_graph: InstantiatedGraphImpl,
}

#[wasm_bindgen(js_class = InstantiatedGraph)]
impl InstantiatedGraphWasm {
    /// Create a new InstantiatedGraphWasm by:
    ///  - Deserializing a `DefGraph` from JSON,
    ///  - Parsing the stream/inlet overrides from JS objects,
    ///  - Constructing the internal Rust `InstantiatedGraph`.
    #[wasm_bindgen(constructor)]
    pub fn new(
        context_id: String,               // formerly "contextId"
        def_graph_json: String,           // pass the serialized DefGraph JSON
        root_job_id: String,              // root job name
        streamIdOverrides: JsValue,       // a JS object => parse into HashMap<String,String>
        inletHasTransformOverridesByTag: JsValue, // a JS object => parse into HashMap<String,bool>
        streamSourceSpecTypeByStreamId: JsValue,  // a JS object => parse into HashMap<String,(String,String)>
    ) -> Result<InstantiatedGraphWasm, JsError> {
        // For better rust panic messages in the JS console (optional):
        set_panic_hook();

        // 1) Reconstruct the DefGraph from the JSON string.
        let def_graph = load_def_graph_from_json_impl(def_graph_json);

        // 2) Parse the override maps from JS Values --> Rust HashMaps:
        let parsed_stream_id_overrides: HashMap<String, String> =
            from_value(streamIdOverrides).map_err(|e| JsError::new(&e.to_string()))?;
        let parsed_inlet_transform_map: HashMap<String, bool> =
            from_value(inletHasTransformOverridesByTag).map_err(|e| JsError::new(&e.to_string()))?;
        let parsed_source_spec_type_map: HashMap<String, StreamSourceSpecType> =
            from_value(streamSourceSpecTypeByStreamId).map_err(|e| JsError::new(&e.to_string()))?;

        // 3) Build our Rust InstantiatedGraph from the reconstructed DefGraph + overrides:
        let inst_graph = InstantiatedGraphImpl::new(
            context_id,
            root_job_id,
            parsed_stream_id_overrides,
            parsed_inlet_transform_map,
            parsed_source_spec_type_map,
            &def_graph,
        );

        Ok(InstantiatedGraphWasm { inst_graph })
    }

    /// Retrieve all node IDs in the InstantiatedGraph (JS array of u32).
    #[wasm_bindgen(js_name = nodes)]
    pub fn nodes(&self) -> Vec<Number> {
        self.inst_graph.node_indices().into_iter().map(|n| Number::from(n)).collect()
    }

    /// Return the node's attributes as a JS object.  
    /// Similar to the TS `InstantiatedGraph.getNodeAttributes(...)`.
    #[wasm_bindgen(js_name = getNodeAttributes)]
    pub fn get_node_attributes(&self, node_id: u32) -> InstantiatedGraphNodeWasm {
        let node = self
            .inst_graph
            .node_weight(node_id);

        match node {
            Some(node) => {
                // Map InstantiatedNodeTypeImpl => InstantiatedNodeType for Wasm:
        let node_type = match node.node_type {
            InstantiatedNodeTypeImpl::RootJob => InstantiatedNodeType::RootJob,
            InstantiatedNodeTypeImpl::Job => InstantiatedNodeType::Job,
            InstantiatedNodeTypeImpl::Stream => InstantiatedNodeType::Stream,
            InstantiatedNodeTypeImpl::Inlet => InstantiatedNodeType::Inlet,
            InstantiatedNodeTypeImpl::Outlet => InstantiatedNodeType::Outlet,
            InstantiatedNodeTypeImpl::Alias => InstantiatedNodeType::Alias,
        };

        return InstantiatedGraphNodeWasm {
            node_type,
            job_id: node.job_id.clone(),
            spec_name: node.spec_name.clone(),
            unique_spec_label: node.unique_spec_label.clone(),
            stream_id: node.stream_id.clone(),
            tag: node.tag.clone(),
            has_transform: node.has_transform,
                    alias: node.alias.clone(),
                    direction: node.direction.clone(),
                    label: node.label.clone(),
                };
            }
            None => {
                panic!("Failed to get node attributes for node id {}", node_id);
            }
        }
    }

    /// Return the total number of edges in the InstantiatedGraph (optional).
    #[wasm_bindgen(js_name = edgeCount)]
    pub fn edge_count(&self) -> usize {
        self.inst_graph.edge_count()
    }

    /// Retrieve the "source" and "targets" for a particular stream node,
    /// returning a JS object with shape `{ source: { origin, outlet_node }, targets: [...] }`.
    #[wasm_bindgen(js_name = getNodesConnectedToStream)]
    pub fn get_nodes_connected_to_stream(&self, stream_node_id: u32) -> Result<JsValue, JsError> {
        let connections = self.inst_graph.get_nodes_connected_to_stream(stream_node_id);

        let source_wasm = connections.source.map(|src| {
            InstantiatedStreamConnectionSourceWasm {
                origin: self.graph_node_to_wasm(&src.origin),
                outlet_node: self.graph_node_to_wasm(&src.outlet_node),
            }
        });

        let target_wasm = connections
            .targets
            .into_iter()
            .map(|t| InstantiatedStreamConnectionTargetWasm {
                inlet_node: self.graph_node_to_wasm(&t.inlet_node),
                destination: self.graph_node_to_wasm(&t.destination),
            })
            .collect::<Vec<_>>();

        let connections_wasm = InstantiatedStreamConnectionsWasm {
            source: source_wasm,
            targets: target_wasm,
        };

        to_value(&connections_wasm).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Return an array of node IDs that are inbound neighbors of the given node ID.
    #[wasm_bindgen(js_name = inboundNeighbors)]
    pub fn inbound_neighbors(&self, node_id: u32) -> Vec<u32> {
        self.inst_graph.inbound_neighbors(node_id)
    }

    /// getSourceSpecNodeConnectedToStream
    #[wasm_bindgen(js_name = getSourceSpecNodeConnectedToStream)]
    pub fn get_source_spec_node_connected_to_stream(&self, stream_node_id: u32) -> Option<InstantiatedStreamConnectionSourceWasm> {
        self.inst_graph.get_source_spec_node_connected_to_stream(stream_node_id).map(|src| {
            InstantiatedStreamConnectionSourceWasm {
                origin: self.graph_node_to_wasm(&src.origin),
                outlet_node: self.graph_node_to_wasm(&src.outlet_node),
            }
        })
    }

    /// Just like the TS version: find a stream node by matching a jobId, direction, and tag.
    #[wasm_bindgen(js_name = findStreamNodeIdConnectedToJob)]
    pub fn find_stream_node_id_connected_to_job(
        &self,
        job_id: String,
        direction: String,
        tag: String,
    ) -> Option<u32> {
        self.inst_graph
            .find_stream_node_id_connected_to_job(&job_id, &direction, &tag)
    }

    /// Return an array of edge IDs that enter the given nodeId.
    /// Mirrors InstantiatedGraph::inbound_edges.
    #[wasm_bindgen(js_name = inboundEdges)]
    pub fn inbound_edges(&self, node_id: u32) -> Vec<Number> {
        self.inst_graph.inbound_edges(node_id).into_iter().map(|e| Number::from(e)).collect()
    }

    /// Return an array of edge IDs that leave the given nodeId.
    /// Mirrors InstantiatedGraph::outbound_edges.
    #[wasm_bindgen(js_name = outboundEdges)]
    pub fn outbound_edges(&self, node_id: u32) -> Vec<Number> {
        self.inst_graph.outbound_edges(node_id).into_iter().map(|e| Number::from(e)).collect()
    }

    /// Return the source node ID of the given edge ID.
    #[wasm_bindgen(js_name = source)]
    pub fn source(&self, edge_id: u32) -> u32 {
        self.inst_graph.source(edge_id)
    }

    /// Return the target node ID of the given edge ID.
    #[wasm_bindgen(js_name = target)]
    pub fn target(&self, edge_id: u32) -> u32 {
        self.inst_graph.target(edge_id)
    }

    /// Return the root job ID string, which is stored in the Rust InstantiatedGraph.
    #[wasm_bindgen(getter, js_name = rootJobId)]
    pub fn root_job_id(&self) -> String {
        self.inst_graph.root_job_id.clone()
    }

    /// Return the root job ID string, which is stored in the Rust InstantiatedGraph.
    #[wasm_bindgen(getter, js_name = streamSourceSpecTypeByStreamId)]
    pub fn stream_source_spec_type_by_stream_id(&self) -> Result<JsValue, JsError> {
        let dict = self.inst_graph.stream_source_spec_type_by_stream_id.clone();
        serde_wasm_bindgen::to_value(&dict).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Return the root job node as a JS object.
    #[wasm_bindgen(getter, js_name = rootJobNodeId)]
    pub fn root_job_node_id(&self) -> u32 {
        self.inst_graph.get_root_job_node_id()
    }

    /// Return the serialized JSON string of the InstantiatedGraph.
    #[wasm_bindgen(js_name = toJson)]
    pub fn to_json(&self) -> String {
        self.inst_graph.to_json().expect("Failed to serialize InstantiatedGraph to JSON")
    }

    /// Return an array of edge IDs.
    #[wasm_bindgen(js_name = edgesPrintout)]
    pub fn edges_printout(&self) -> String {
        // format: "(source -> target), (source -> target), ..."
        let edges = self.inst_graph.raw_edges();
        edges.into_iter().map(|e| format!("({} -> {})", e[0], e[1])).collect::<Vec<_>>().join(", ")
    }

}

// Helper for converting from internal Rust node -> Wasm-friendly node
impl InstantiatedGraphWasm {
    fn graph_node_to_wasm(&self, node: &InstantiatedGraphNodeImpl) -> InstantiatedGraphNodeWasm {
        let node_type = match node.node_type {
            InstantiatedNodeTypeImpl::RootJob => InstantiatedNodeType::RootJob,
            InstantiatedNodeTypeImpl::Job => InstantiatedNodeType::Job,
            InstantiatedNodeTypeImpl::Stream => InstantiatedNodeType::Stream,
            InstantiatedNodeTypeImpl::Inlet => InstantiatedNodeType::Inlet,
            InstantiatedNodeTypeImpl::Outlet => InstantiatedNodeType::Outlet,
            InstantiatedNodeTypeImpl::Alias => InstantiatedNodeType::Alias,
        };

        InstantiatedGraphNodeWasm {
            node_type,
            job_id: node.job_id.clone(),
            spec_name: node.spec_name.clone(),
            unique_spec_label: node.unique_spec_label.clone(),
            stream_id: node.stream_id.clone(),
            tag: node.tag.clone(),
            has_transform: node.has_transform,
            alias: node.alias.clone(),
            direction: node.direction.clone(),
            label: node.label.clone(),
        }
    }
} 