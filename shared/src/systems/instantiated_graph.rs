use crate::systems::def_graph::{
    DefGraph, /* DefGraphNode, */ DefGraphNodeType, 
    StreamConnectionSource, StreamConnectionTarget,
};
use crate::systems::def_graph_utils::unique_spec_identifier;
use petgraph::graph::{DiGraph, NodeIndex, EdgeIndex};
use petgraph::visit::EdgeRef;
use petgraph::Direction;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, PartialEq, Serialize, Deserialize, Clone)]
#[serde(rename_all = "kebab-case")]
pub enum InstantiatedNodeType {
    RootJob,
    Job,
    Stream,
    Inlet,
    Outlet,
    Alias,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
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

    /// Each node retains a `label` from the original DefGraph node or a generated one.
    pub label: String,
}

/// Stores a "source" specification for a stream: which job node (origin)
/// and which Outlet node is connected.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstantiatedStreamConnectionSource {
    pub origin: InstantiatedGraphNode,
    pub outlet_node: InstantiatedGraphNode,
}

/// Stores a "target" specification for a stream: which Inlet node
/// and which job node (destination) is connected.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstantiatedStreamConnectionTarget {
    pub inlet_node: InstantiatedGraphNode,
    pub destination: InstantiatedGraphNode,
}

/// Summarizes the single source (if any) and all target connections for a given stream node.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstantiatedStreamConnections {
    pub source: Option<InstantiatedStreamConnectionSource>,
    pub targets: Vec<InstantiatedStreamConnectionTarget>,
}


#[derive(Clone, Serialize, Deserialize, Debug)]
#[serde(rename_all = "camelCase")]
pub struct StreamSourceSpecType {
    pub spec_name: String,
    pub tag: String,
}

#[derive(Serialize, Deserialize, Debug)]
#[serde(rename_all = "camelCase")]
pub struct InstantiatedGraph {
    /// Internal directed graph of InstantiatedGraphNodes.
    graph: DiGraph<InstantiatedGraphNode, ()>,

    /// Mapping from an old DefGraph node's label to the new node index.
    node_indices: HashMap<String, NodeIndex>,

    /// Mapping from a new node index to the old DefGraph node's label.
    inverse_node_indices: HashMap<NodeIndex, String>,

    /// The original DefGraph.
    pub def_graph: DefGraph,

    /// The context ID for generating job IDs, e.g. "[contextId]SpecName".
    pub context_id: String,

    /// Root job ID used for the RootSpec node.
    pub root_job_id: String,

    /// Named overrides for streams: "in/someTag" or "out/someTag" => replacement string.
    pub stream_id_overrides: HashMap<String, String>,

    /// Tag -> bool overrides to force has_transform on specific Inlet nodes.
    pub inlet_has_transform_overrides_by_tag: HashMap<String, bool>,

    /// Optional - store e.g. (specName, tag) for a given stream ID (unused in the basic tests).
    pub stream_source_spec_type_by_stream_id: HashMap<String, StreamSourceSpecType>,
}

impl InstantiatedGraph {
    /// Creates a new InstantiatedGraph from the given DefGraph, storing the context,
    /// root job ID, and override maps. Instantiation is performed immediately.
    pub fn new(
        context_id: String,
        root_job_id: String,
        stream_id_overrides: HashMap<String, String>,
        inlet_has_transform_overrides_by_tag: HashMap<String, bool>,
        stream_source_spec_type_by_stream_id: HashMap<String, StreamSourceSpecType>,
        def_graph: &DefGraph,
    ) -> Self {
        let graph = DiGraph::<InstantiatedGraphNode, ()>::new();
        let node_indices = HashMap::new();
        let inverse_node_indices = HashMap::new();
        let mut instantiated_graph = InstantiatedGraph {
            graph,
            node_indices,
            inverse_node_indices,
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

    /// Core routine that does a multi-pass creation of the InstantiatedGraph.
    ///  - In pass 1, we convert each DefGraph node into an InstantiatedGraph node or a suitable ID,
    ///    replicating the logic from the original TypeScript InstantiatedGraph.ts
    ///  - In pass 2, we re-map edges similarly, hooking them up to the new IDs/nodes.
    fn instantiate(&mut self) {
        let node_ids = self.def_graph.node_indices();

        // Holds the newly generated "job node ID" for each old Spec node index.
        let mut child_job_node_by_node_index: HashMap<u32, String> = HashMap::new();
        // Holds the newly generated "stream node ID" for each old StreamDef node index.
        let mut stream_node_by_node_index: HashMap<u32, String> = HashMap::new();

        // First pass: create or figure out the new node IDs for each DefGraph node.
        for &old_index in &node_ids {
            let node_data = self
                .def_graph
                .node_weight(old_index)
                .expect("DefGraph node missing")
                .clone();

            let def_node_id_str = old_index.to_string();

            match node_data.node_type {
                DefGraphNodeType::RootSpec => {
                    // => "root-job"
                    let new_node = InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::RootJob,
                        job_id: Some(self.root_job_id.clone()),
                        spec_name: node_data.spec_name.clone(),
                        unique_spec_label: node_data.unique_spec_label.clone(),
                        stream_id: None,
                        tag: None,
                        has_transform: None,
                        alias: None,
                        direction: None,
                        label: node_data.label.clone(),
                    };
                    let idx = self.graph.add_node(new_node);
                    self.node_indices.insert(self.root_job_id.clone(), idx);
                    self.inverse_node_indices.insert(idx, self.root_job_id.clone());
                }
                DefGraphNodeType::Spec => {
                    let spec_identifier = unique_spec_identifier(
                        node_data.spec_name.clone().unwrap_or_else(|| {
                        panic!("spec_name is None");
                    }), 
                    node_data.unique_spec_label.clone());
                    let job_id = format!("[{}]{}", self.context_id, spec_identifier);
                    child_job_node_by_node_index.insert(old_index, job_id.clone());

                    let new_node = InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Job,
                        job_id: Some(job_id.clone()),
                        spec_name: node_data.spec_name.clone(),
                        unique_spec_label: node_data.unique_spec_label.clone(),
                        stream_id: None,
                        tag: None,
                        has_transform: None,
                        alias: None,
                        direction: None,
                        label: job_id.clone(),
                    };
                    let idx = self.graph.add_node(new_node);
                    // We store the old node label => new index, so edges can be matched up later.
                    self.node_indices.insert(job_id.clone(), idx);
                    self.inverse_node_indices.insert(idx, job_id.clone());
                }
                DefGraphNodeType::StreamDef => {
                    // => "stream" with ID possibly auto-generated or overridden.
                    let connections = self.def_graph.get_nodes_connected_to_stream(old_index);
                    // We'll now try to see if there's an out/{tag} override if source is RootSpec,
                    // or an in/{tag} override from the first target. If neither, fallback to "[contextId]streamDefId".
                    let mut chosen_stream_id: Option<String> = None;

                    // Check if there's a rootSpec source => override out/{tag}
                    if let Some(StreamConnectionSource {
                        origin,
                        outlet_node,
                    }) = connections.source
                    {
                        if origin.node_type == DefGraphNodeType::RootSpec {
                            if let Some(out_tag) = outlet_node.tag.clone() {
                                let override_key = format!("out/{}", out_tag);
                                if let Some(override_val) = self.stream_id_overrides.get(&override_key) {
                                    chosen_stream_id = Some(override_val.clone());
                                }
                            }
                        }
                    }

                    // If not found, check for an in/{tag} override among targets
                    if chosen_stream_id.is_none() {
                        for StreamConnectionTarget { inlet_node, .. } in connections.targets {
                            if let Some(in_tag) = inlet_node.tag.clone() {
                                let override_key = format!("in/{}", in_tag);
                                if let Some(override_val) = self.stream_id_overrides.get(&override_key)
                                {
                                    chosen_stream_id = Some(override_val.clone());
                                    break;
                                }
                            }
                        }
                    }

                    // If still none, default to "[{contextId}]{streamDefId or label}"
                    if chosen_stream_id.is_none() {
                        // assume node_data.stream_def_id exists or throw 
                        let fallback_id = node_data
                            .stream_def_id
                            .clone()
                            .unwrap_or_else(|| {
                                panic!("stream_def_id is None");
                            });
                        let default_stream_id = format!("[{}]{}", self.context_id, fallback_id);
                        chosen_stream_id = Some(default_stream_id);
                    }

                    let final_stream_id = chosen_stream_id.unwrap();
                    // Store for second pass

                    // Actually create the node in the graph if we haven't yet done so.
                    // But our approach is "one node per old label." If multiple StreamDefs share
                    // the same final_stream_id, we only want one node. So let's see if we've created it:
                    if !self.node_indices.contains_key(&final_stream_id) {
                        let new_node = InstantiatedGraphNode {
                            node_type: InstantiatedNodeType::Stream,
                            job_id: None,
                            spec_name: None,
                            unique_spec_label: None,
                            stream_id: Some(final_stream_id.clone()),
                            tag: None,
                            has_transform: None,
                            alias: None,
                            direction: None,
                            label: final_stream_id.clone(),
                        };
                        let idx = self.graph.add_node(new_node);
                        // We can't store old label => idx in `node_indices`, because multiple
                        // StreamDefs might produce the same final_stream_id. So store the *old* label => idx
                        // to "reserve" that label. Then edges can look up the final ID from `stream_node_by_node_index`.
                        // We'll do the final edge pass next. 
                        self.node_indices.insert(final_stream_id.clone(), idx);
                        self.inverse_node_indices.insert(idx, final_stream_id.clone());
                    } 
                    stream_node_by_node_index.insert(old_index, final_stream_id.clone());
                }
                DefGraphNodeType::Inlet => {
                    // Possibly apply has_transform overrides
                    let override_transform = if let Some(tag) = &node_data.tag {
                        self.inlet_has_transform_overrides_by_tag.get(tag).copied()
                    } else {
                        None
                    };
                    let new_node = InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Inlet,
                        job_id: None,
                        spec_name: None,
                        unique_spec_label: None,
                        stream_id: None,
                        tag: node_data.tag.clone(),
                        has_transform: override_transform.or(node_data.has_transform),
                        alias: None,
                        direction: None,
                        label: node_data.label.clone(),
                    };
                    let idx = self.graph.add_node(new_node);
                    self.node_indices.insert(def_node_id_str.clone(), idx);
                    self.inverse_node_indices.insert(idx, def_node_id_str.clone());
                }
                DefGraphNodeType::Outlet => {
                    let new_node = InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Outlet,
                        job_id: None,
                        spec_name: None,
                        unique_spec_label: None,
                        stream_id: None,
                        tag: node_data.tag.clone(),
                        has_transform: None,
                        alias: None,
                        direction: None,
                        label: node_data.label.clone(),
                    };
                    let idx = self.graph.add_node(new_node);
                    self.node_indices.insert(def_node_id_str.clone(), idx);
                    self.inverse_node_indices.insert(idx, def_node_id_str.clone());
                }
                DefGraphNodeType::Alias => {
                    let new_node = InstantiatedGraphNode {
                        node_type: InstantiatedNodeType::Alias,
                        job_id: None,
                        spec_name: None,
                        unique_spec_label: None,
                        stream_id: None,
                        tag: None,
                        has_transform: None,
                        alias: node_data.alias.clone(),
                        direction: node_data.direction.clone(),
                        label: node_data.label.clone(),
                    };
                    let idx = self.graph.add_node(new_node);
                    self.node_indices.insert(def_node_id_str.clone(), idx);
                    self.inverse_node_indices.insert(idx, def_node_id_str.clone());
                }
            }
        }

        // Second pass: Re-map edges. This matches the TypeScript: we adjust the from/to if they're
        // a Spec => child_job_node_by_node_index, a RootSpec => root_job_id, or a StreamDef => stream_node_by_node_index, etc.
        let edge_list = self.def_graph.raw_edges();
        for (from_index, to_index) in edge_list {
            let from_node = self.def_graph.node_weight(from_index).unwrap();
            let to_node = self.def_graph.node_weight(to_index).unwrap();

            let new_from = match from_node.node_type {
                DefGraphNodeType::Spec => {
                    // Use the job node we made for that old index
                    if let Some(job_node_id) = child_job_node_by_node_index.get(&from_index) {
                        job_node_id.clone()
                    } else {
                        // fallback: use from_index converted to string
                        // cast from_index to NodeIndex
                        let from_index_node_index = NodeIndex::new(from_index as usize);
                        self.inverse_node_indices.get(&from_index_node_index).unwrap().to_string()
                    }
                }
                DefGraphNodeType::RootSpec => self.root_job_id.clone(),
                DefGraphNodeType::StreamDef => {
                    if let Some(stream_node_id) = stream_node_by_node_index.get(&from_index) {
                        stream_node_id.clone()
                    } else {
                        // cast from_index to NodeIndex
                        let from_index_node_index = NodeIndex::new(from_index as usize);
                        self.inverse_node_indices.get(&from_index_node_index).unwrap().to_string()
                    }
                }
                // For all else, just use the old label
                _ => {
                    // cast from_index to NodeIndex
                    let from_index_node_index = NodeIndex::new(from_index as usize);
                    self.inverse_node_indices.get(&from_index_node_index).unwrap().to_string()
                }
            };

            let new_to = match to_node.node_type {
                DefGraphNodeType::Spec => {
                    if let Some(job_node_id) = child_job_node_by_node_index.get(&to_index) {
                        job_node_id.clone()
                    } else {
                        // cast to_index to NodeIndex
                        let to_index_node_index = NodeIndex::new(to_index as usize);
                        self.inverse_node_indices.get(&to_index_node_index).unwrap().to_string()
                    }
                }
                DefGraphNodeType::RootSpec => self.root_job_id.clone(),
                DefGraphNodeType::StreamDef => {
                    if let Some(stream_node_id) = stream_node_by_node_index.get(&to_index) {
                        stream_node_id.clone()
                    } else {
                        // cast to_index to NodeIndex
                        let to_index_node_index = NodeIndex::new(to_index as usize);
                        self.inverse_node_indices.get(&to_index_node_index).unwrap().to_string()
                    }
                }
                _ => {
                    // cast to_index to NodeIndex
                    let to_index_node_index = NodeIndex::new(to_index as usize);
                    self.inverse_node_indices.get(&to_index_node_index).unwrap().to_string()
                }
            };

            // Add the edge if it doesn't exist
            if let (Some(f_idx), Some(t_idx)) =
                (self.node_indices.get(&new_from), self.node_indices.get(&new_to))
            {
                if !self.graph.contains_edge(*f_idx, *t_idx) {
                    self.graph.add_edge(*f_idx, *t_idx, ());
                }
            } else {
                // panic if we don't have the node indices
                if !self.node_indices.contains_key(&new_from) {
                    // println!("node_indices: {:?}", self.node_indices);
                    panic!("Node index not found for new_from: {}. Available node indices: {:?}", new_from, self.node_indices);
                }
                if !self.node_indices.contains_key(&new_to) {
                    // println!("node_indices: {:?}", self.node_indices);
                    panic!("Node index not found for new_to: {}. Available node indices: {:?}", new_to, self.node_indices);
                }
            }
        }
    }

    /// Return an InstantiatedGraphNode reference for a given node index, if it exists.
    pub fn node_weight(&self, index: u32) -> Option<&InstantiatedGraphNode> {
        let idx = NodeIndex::new(index as usize);
        self.graph.node_weight(idx)
    }

    /// Number of edges in the InstantiatedGraph.
    pub fn edge_count(&self) -> usize {
        self.graph.edge_count()
    }

    /// Return all node indices (as *u32*) for iteration.
    pub fn node_indices(&self) -> Vec<u32> {
        self.graph.node_indices().map(|n| n.index() as u32).collect()
    }

  

   

    // -------------------------------------------
    // The following methods replicate the TS logic
    // for analyzing the structure of an *instantiated* stream node
    // (i.e. "Stream" node type), retrieving connected Inlet/Outlet/Job nodes, etc.
    // -------------------------------------------

    /// Returns the "source" of a given stream (the job node that leads to an Outlet, if any).
    /// In TS: getSourceSpecNodeConnectedToStream
    pub fn get_source_spec_node_connected_to_stream(
        &self,
        stream_node_id: u32,
    ) -> Option<InstantiatedStreamConnectionSource> {

        // Get source: from stream node, use inbound_neighbors to find an Outlet node
        let inbound_ids = self.inbound_neighbors(stream_node_id);
        let outlet_id_option = inbound_ids.into_iter().find(|&id| {
            if let Some(node) = self.node_weight(id) {
                node.node_type == InstantiatedNodeType::Outlet
            } else {
                false
            }
        });
        let source = if let Some(outlet_id) = outlet_id_option {
            let spec_ids = self.inbound_neighbors(outlet_id);
            if let Some(source_spec_id) = spec_ids.into_iter().find(|&id| {
                if let Some(node) = self.node_weight(id) {
                    node.node_type == InstantiatedNodeType::Job || node.node_type == InstantiatedNodeType::RootJob
                } else {
                    false
                }
            }) {
                if let (Some(outlet_node), Some(origin_node)) = (self.node_weight(outlet_id), self.node_weight(source_spec_id)) {
                    Some(InstantiatedStreamConnectionSource {
                        origin: origin_node.clone(),
                        outlet_node: outlet_node.clone(),
                    })
                } else {
                    None
                }
            } else {
                None
            }
        } else {
            None
        };
        
        return source;
    }

    pub fn inbound_neighbors(&self, node_id: u32) -> Vec<u32> {
        let node_id = NodeIndex::new(node_id as usize);
        self.graph
            .neighbors_directed(node_id, petgraph::Incoming)
            .map(|index| index.index() as u32)
            .collect()
    }

    /// Returns the "target" nodes for a given stream (Inlet nodes + job node that follows after).
    /// In TS: getTargetSpecNodesConnectedToStream
    pub fn get_target_spec_nodes_connected_to_stream(
        &self,
        stream_node_id: u32,
    ) -> Vec<InstantiatedStreamConnectionTarget> {
        let mut result = Vec::new();

        // find all outbound neighbors that are Inlet nodes
        let inlet_ids = self.outbound_neighbors_of_type(stream_node_id, InstantiatedNodeType::Inlet);
        for inlet_id in inlet_ids {
            if let Some(inlet_node) = self.node_weight(inlet_id).cloned() {
                // now find the outbound neighbor of that inlet that is RootJob / Job
                let job_ids = self.outbound_neighbors_of_type_multiple(
                    inlet_id,
                    &[InstantiatedNodeType::RootJob, InstantiatedNodeType::Job],
                );
                for j_id in job_ids {
                    if let Some(destination) = self.node_weight(j_id).cloned() {
                        result.push(InstantiatedStreamConnectionTarget { inlet_node: inlet_node.clone(), destination });
                    }
                }
            }
        }
        result
    }

    /// Combines the above two methods, returning source + targets for the stream.
    /// In TS: getNodesConnectedToStream
    pub fn get_nodes_connected_to_stream(
        &self,
        stream_node_id: u32,
    ) -> InstantiatedStreamConnections {
        let source = self.get_source_spec_node_connected_to_stream(stream_node_id);
        let targets = self.get_target_spec_nodes_connected_to_stream(stream_node_id);
        InstantiatedStreamConnections { source, targets }
    }

    /// Replicates findStreamNodeIdConnectedToJob({ jobId, type, tag }) from the TS version.
    /// We search for the first "Stream" node that matches the desired in/out connection.
    /// - If `type == "out"`, we check the stream's source to see if it's from a RootJob node
    ///   with the correct outlet `tag`.
    /// - If `type == "in"`, we see if at least one target is a job node with `job_id == jobId`
    ///   and the inlet has `tag`.
    /// Returns the final *stream node's label* if found, or None.
    pub fn find_stream_node_id_connected_to_job(
        &self,
        job_id: &str,
        direction: &str,
        tag: &str,
    ) -> Option<u32> {
        // Search over all nodes for node_type == Stream
        for node_idx in self.graph.node_indices() {
            if let Some(node) = self.graph.node_weight(node_idx) {
                if node.node_type == InstantiatedNodeType::Stream {
                    // We'll examine the connections for this stream
                    let stream_id = node_idx.index() as u32;
                    let connections = self.get_nodes_connected_to_stream(node_idx.index() as u32);

                    match direction {
                        "out" => {
                            // Must come from rootJob outgoing with an outlet node that has the correct tag
                            if let Some(src) = connections.source {
                                // Check if the source node is root-job
                                if src.origin.node_type == InstantiatedNodeType::RootJob
                                    && src.outlet_node.tag.as_deref() == Some(tag)
                                {
                                    return Some(stream_id);
                                }
                            }
                        }
                        "in" => {
                            // Must go to a job that has job_id = jobId with an inlet that has the correct tag
                            let matched_target = connections.targets.iter().any(|t| {
                                t.destination.job_id.as_deref() == Some(job_id)
                                    && t.inlet_node.tag.as_deref() == Some(tag)
                            });
                            if matched_target {
                                return Some(stream_id);
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        None
    }

    
   

    /// Return the outbound neighbors of `node_id` that match one particular node type.
    fn outbound_neighbors_of_type(
        &self,
        node_id: u32,
        node_type: InstantiatedNodeType,
    ) -> Vec<u32> {
        let n_idx = NodeIndex::new(node_id as usize);
        let mut results = Vec::new();
        for neighbor in self.graph.neighbors_directed(n_idx, petgraph::Outgoing) {
            if let Some(n) = self.graph.node_weight(neighbor) {
                if n.node_type == node_type {
                    results.push(neighbor.index() as u32);
                }
            }
        }
        results
    }

    /// Return the outbound neighbors of `node_id` if they match *any* of the node types in the slice.
    fn outbound_neighbors_of_type_multiple(
        &self,
        node_id: u32,
        types: &[InstantiatedNodeType],
    ) -> Vec<u32> {
        let n_idx = NodeIndex::new(node_id as usize);
        let mut results = Vec::new();
        for neighbor in self.graph.neighbors_directed(n_idx, petgraph::Outgoing) {
            if let Some(n) = self.graph.node_weight(neighbor) {
                if types.contains(&n.node_type) {
                    results.push(neighbor.index() as u32);
                }
            }
        }
        results
    }

    // --------------------------------------------------------------------
    // NEW: Provide inboundEdges / outboundEdges returning "edge IDs."
    // In Petgraph, edges have an internal EdgeIndex. We can expose that as u32.
    // --------------------------------------------------------------------

    /// Return a list of edge IDs whose target is `node_id`.
    pub fn inbound_edges(&self, node_id: u32) -> Vec<u32> {
        let n_idx = NodeIndex::new(node_id as usize);
        // Gather each incoming edge's "edge index"
        self.graph
            .edges_directed(n_idx, Direction::Incoming)
            .map(|edge_ref| edge_ref.id().index() as u32)
            .collect()
    }

    /// Return a list of edge IDs whose source is `node_id`.
    pub fn outbound_edges(&self, node_id: u32) -> Vec<u32> {
        let n_idx = NodeIndex::new(node_id as usize);
        self.graph
            .edges_directed(n_idx, Direction::Outgoing)
            .map(|edge_ref| edge_ref.id().index() as u32)
            .collect()
    }

    // --------------------------------------------------------------------
    // NEW: Provide source(edgeId) and target(edgeId)
    // --------------------------------------------------------------------
    /// Given an "edge ID," return the node ID of that edge's source.
    pub fn source(&self, edge_id: u32) -> u32 {
        let e_idx = EdgeIndex::new(edge_id as usize);
        let (source_idx, _target_idx) = self
            .graph
            .edge_endpoints(e_idx)
            .expect("Invalid edge ID or missing endpoints");
        source_idx.index() as u32
    }

    /// Given an "edge ID," return the node ID of that edge's target.
    pub fn target(&self, edge_id: u32) -> u32 {
        let e_idx = EdgeIndex::new(edge_id as usize);
        let (_source_idx, target_idx) = self
            .graph
            .edge_endpoints(e_idx)
            .expect("Invalid edge ID or missing endpoints");
        target_idx.index() as u32
    }

    pub fn raw_edges(&self) -> Vec<Vec<u32>> {
        self.graph.edge_indices().map(|e| {
            let (source_idx, target_idx) = self.graph.edge_endpoints(e).expect("Invalid edge ID or missing endpoints");
            vec![source_idx.index() as u32, target_idx.index() as u32]
        }).collect()
    }

    // --------------------------------------------------------------------
    // Optionally, you might want a direct method to get root_job_id
    // but it's already stored in the "root_job_id" field as `pub`.
    // If you want a function, do this:
    // --------------------------------------------------------------------
    pub fn get_root_job_id(&self) -> &str {
        &self.root_job_id
    }

    pub fn get_root_job_node_id(&self) -> u32 {
        let node_idx = self.node_indices.get(&self.root_job_id).expect("Node not found");
        node_idx.index() as u32
    }

    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(&self)
    }
    
}
