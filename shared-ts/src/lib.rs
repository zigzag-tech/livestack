#![deny(clippy::all)]
use napi_derive::napi;


use livestack_shared::systems::def_graph_utils::{
  unique_spec_identifier,
  unique_stream_identifier as unique_stream_identifier_impl, SpecTagInfo as SpecTagInfoImpl,
  FromSpecAndTag as FromSpecAndTagImpl,
  ToSpecAndTag as ToSpecAndTagImpl,
  
};

use livestack_shared::systems::def_graph::{
  DefGraph as DefGraphImpl,
  load_from_json as load_from_json_impl,
  NodeType as NodeTypeImpl,
  
};


// #[macro_use]
// extern crate napi_derive;

#[napi]
pub fn gen_spec_identifier(spec_name: String, unique_spec_label: Option<String>) -> String {
  return unique_spec_identifier(spec_name, unique_spec_label);
}



#[napi(object)]
pub struct SpecTagInfoParams {
  pub spec_name: String,
  pub unique_spec_label: Option<String>,
  pub tag: String,
}
#[napi(object)]
pub struct UniqueStreamIdentifierParams {
  pub from: Option<SpecTagInfoParams>,
  pub to: Option<SpecTagInfoParams>,
}


#[napi]
pub fn unique_stream_identifier(p: UniqueStreamIdentifierParams) -> String {
  let from = match p.from {
    Some(from) => {
      Some(SpecTagInfoImpl {
        spec_name: from.spec_name,
        unique_spec_label: from.unique_spec_label,
        tag: from.tag,
      })
    }
    None => None,
  };
  let to = match p.to {
    Some(to) => {
      Some(SpecTagInfoImpl {
        spec_name: to.spec_name,
        unique_spec_label: to.unique_spec_label,
        tag: to.tag,
      })
    }
    None => None,
  };
  return unique_stream_identifier_impl(from, to);
}

#[napi]
pub struct DefGraph {
   def_graph: DefGraphImpl,
}


#[napi(object)]
pub struct DefGraphSpecParams {
  pub name: String,
  pub input_def_set: DefSetParams,
  pub output_def_set: DefSetParams,
}
#[napi(object)]
pub struct DefGraphParams {
  pub root: DefGraphSpecParams
}

#[napi(object)]
pub struct DefSetParams {
  pub tags: Vec<String>,
}

#[napi]
pub enum NodeType {
  RootSpec,
  Spec,
  StreamDef,
  Inlet,
  Outlet,
  Alias,
}
#[napi(object)]
pub struct DefGraphNode {
  pub id: u32,
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

#[napi(object)]
pub struct GetInboundNodeSetsResult {
  pub inlet_node: DefGraphNode,
  pub stream_node: DefGraphNode,
}

#[napi(object)]
pub struct GetOutboundNodeSetsResult {
  pub outlet_node: DefGraphNode,
  pub stream_node: DefGraphNode,
}

#[napi(object)]
pub struct RawEdge {
  pub source: u32,
  pub target: u32,
}

#[napi(object)]
pub struct FromSpecAndTag {
  pub spec_name: String,
  pub output: String,
  pub unique_spec_label: Option<String>,
}

#[napi(object)]
pub struct ToSpecAndTag {
  pub spec_name: String,
  pub input: String,
  pub has_transform: bool,
  pub unique_spec_label: Option<String>,
}

#[napi(object)]
pub struct SpecAndTag {
  pub spec_name: String,
  pub tag: String,
  pub unique_spec_label: Option<String>,
}


#[napi(object)]
pub struct AssignAliasParams {
  pub alias: String,
  pub tag: String,
  pub spec_name: String,
  pub unique_spec_label: Option<String>,
  pub direction: String,
  pub root_spec_name: String,
}

/**
const existingAlias = defG.lookupRootSpecAlias({
      specName,
      tag,
      uniqueSpecLabel,
      type,
    });
 */
#[napi(object)]
pub struct LookUpRootSpecAliasParams {
  pub spec_name: String,
  pub tag: String,
  pub unique_spec_label: Option<String>,
  pub direction: String,
}


#[napi(object)]
pub struct SpecTagInfo {
  pub spec_name: String,
  pub tag: String,
  pub unique_spec_label: Option<String>,
}

#[napi(object)]
pub struct SpecAndTagInfoAndDirection {
  pub spec_name: String,
  pub tag: String,
  pub unique_spec_label: Option<String>,
  pub direction: String,
}

#[napi]
impl DefGraph {

  #[napi(constructor)]
  pub fn new(p: DefGraphParams) -> DefGraph {
    return DefGraph {
      def_graph: DefGraphImpl::new(p.root.name, p.root.input_def_set.tags, p.root.output_def_set.tags),
    };
  }

  #[napi]
  pub fn to_json(&self) -> String {
    return self.def_graph.to_json().expect("Failed to serialize DefGraph to JSON");
  }

  #[napi]
  pub fn get_spec_node_ids(&self) -> Vec<u32> {
    return self.def_graph.get_spec_node_ids()
  }

  #[napi]
  pub fn get_root_spec_node_id(&self) -> u32 {
    let root_id = self.def_graph.get_root_spec_node_id().expect("Failed to get root spec node id");
    // println!("Root id: {}", root_id);
    return root_id;
  }

  #[napi]
  pub fn get_node_attributes(&self, node_id: u32) -> DefGraphNode {
    let node = self.def_graph.node_weight(node_id);
    match node {
      Some(node) => {
        let node_type = match node.node_type {
          NodeTypeImpl::RootSpec => NodeType::RootSpec,
          NodeTypeImpl::Spec => NodeType::Spec,
          NodeTypeImpl::StreamDef => NodeType::StreamDef,
          NodeTypeImpl::Inlet => NodeType::Inlet,
          NodeTypeImpl::Outlet => NodeType::Outlet,
          NodeTypeImpl::Alias => NodeType::Alias,
        };
        return DefGraphNode {
          id: node_id,
          node_type: node_type,
          spec_name: node.spec_name.clone(),
          unique_spec_label: node.unique_spec_label.clone(),
          tag: node.tag.clone(),
          has_transform: node.has_transform,
          stream_def_id: node.stream_def_id.clone(),
          alias: node.alias.clone(),
          direction: node.direction.clone(),
          label: node.label.clone(),
        }
      }
      None => {
        panic!("Failed to get node attributes for node id {}", node_id);
      }
    }
  }

  #[napi]
  pub fn get_inbound_node_sets(&self, node_id: u32) -> Vec<GetInboundNodeSetsResult> {
    let mut results = vec![];
    let inbound_node_sets = self.def_graph.get_inbound_node_sets(node_id);
    for (inlet_node_id, stream_node_id) in inbound_node_sets {
      let inlet_node = self.def_graph.node_weight(inlet_node_id).expect("Failed to get inlet node");
      let stream_node = self.def_graph.node_weight(stream_node_id).expect("Failed to get stream node");
      results.push(GetInboundNodeSetsResult {
        inlet_node: DefGraphNode {
          id: inlet_node_id,
          node_type: NodeType::Inlet,
          spec_name: inlet_node.spec_name.clone(),
          unique_spec_label: inlet_node.unique_spec_label.clone(),
          tag: inlet_node.tag.clone(),
          has_transform: inlet_node.has_transform,
          stream_def_id: inlet_node.stream_def_id.clone(),
          alias: inlet_node.alias.clone(),
          direction: inlet_node.direction.clone(),
          label: inlet_node.label.clone(),
        },
        stream_node: DefGraphNode {
          id: stream_node_id,
          node_type: NodeType::StreamDef,
          spec_name: stream_node.spec_name.clone(),
          unique_spec_label: stream_node.unique_spec_label.clone(),
          tag: stream_node.tag.clone(),
          has_transform: stream_node.has_transform,
          stream_def_id: stream_node.stream_def_id.clone(),
          alias: stream_node.alias.clone(),
          direction: stream_node.direction.clone(),
          label: stream_node.label.clone(),
        },
      });
    }
    return results;
  }

  #[napi]
  pub fn get_outbound_node_sets(&self, node_id: u32) -> Vec<GetOutboundNodeSetsResult> {
    let mut results = vec![];
    let outbound_node_sets = self.def_graph.get_outbound_node_sets(node_id);
    for (outlet_node_id, stream_node_id) in outbound_node_sets {
      let outlet_node = self.def_graph.node_weight(outlet_node_id).expect("Failed to get outlet node");
      let stream_node = self.def_graph.node_weight(stream_node_id).expect("Failed to get stream node");
      results.push(GetOutboundNodeSetsResult {
        outlet_node: DefGraphNode {
          id: outlet_node_id,
          node_type: NodeType::Outlet,
          spec_name: outlet_node.spec_name.clone(),
          unique_spec_label: outlet_node.unique_spec_label.clone(),
          tag: outlet_node.tag.clone(),
          has_transform: outlet_node.has_transform,
          stream_def_id: outlet_node.stream_def_id.clone(),
          alias: outlet_node.alias.clone(),
          direction: outlet_node.direction.clone(),
          label: outlet_node.label.clone(),
        },
        stream_node: DefGraphNode {
          id: stream_node_id,
          node_type: NodeType::StreamDef,
          spec_name: stream_node.spec_name.clone(),
          unique_spec_label: stream_node.unique_spec_label.clone(),
          tag: stream_node.tag.clone(),
          has_transform: stream_node.has_transform,
          stream_def_id: stream_node.stream_def_id.clone(),
          alias: stream_node.alias.clone(),
          direction: stream_node.direction.clone(),
          label: stream_node.label.clone(),
        },
      });
    }
    return results;
  }
  #[napi]
  pub fn nodes(&self) -> Vec<u32> {
    return self.def_graph.node_indices();
  }

  #[napi]
  pub fn inbound_neighbors(&self, node_id: u32) -> Vec<u32> {
    return self.def_graph.inbound_neighbors(node_id);
  }

  #[napi]
  pub fn outbound_neighbors(&self, node_id: u32) -> Vec<u32> {
    return self.def_graph.outbound_neighbors(node_id);
  }

  #[napi]
  pub fn edges(&self) -> Vec<RawEdge> {
    return self.def_graph.raw_edges().iter().map(|edge| {
      return RawEdge {
        source: edge.0,
        target: edge.1,
      }
    }).collect();
  }

  #[napi]
  pub fn add_connected_dual_specs(&mut self, from: FromSpecAndTag, to: ToSpecAndTag) {
    self.def_graph.add_connected_dual_specs(
      &FromSpecAndTagImpl {
        spec_name: from.spec_name,
        output: from.output,
        unique_spec_label: from.unique_spec_label,
      },
      &ToSpecAndTagImpl {
        spec_name: to.spec_name,
        input: to.input,
        has_transform: to.has_transform,
        unique_spec_label: to.unique_spec_label,
      }
    );
  }

  #[napi]
  pub fn ensure_outlet_and_stream(&mut self, s: SpecAndTag ) {
    self.def_graph.ensure_outlet_and_stream(SpecTagInfoImpl {
      spec_name: s.spec_name,
      tag: s.tag,
      unique_spec_label: s.unique_spec_label,
    });
  }  

  #[napi]
  pub fn ensure_inlet_and_stream(&mut self, s: SpecAndTag, has_transform: bool) {
    self.def_graph.ensure_inlet_and_stream(SpecTagInfoImpl {
      spec_name: s.spec_name,
      tag: s.tag,
      unique_spec_label: s.unique_spec_label,
    }, has_transform);
  }

  #[napi]
  pub fn assign_alias(&mut self, p: AssignAliasParams) {
     self.def_graph.assign_alias(&p.alias, &p.spec_name, &p.root_spec_name, p.unique_spec_label.as_deref(), &p.direction, &p.tag);
  }

  #[napi]
  pub fn get_all_alias_node_ids(&self) -> Vec<u32> {
    return self.def_graph.get_all_alias_node_ids();
  }

  #[napi]
  pub fn lookup_root_spec_alias(&self,p: LookUpRootSpecAliasParams) -> Option<String> {
    return self.def_graph.lookup_root_spec_alias(p.spec_name, p.unique_spec_label,p.tag, p.direction);
  }

  #[napi]
  pub fn lookup_spec_and_tag_by_alias(&self, alias: String, direction: String) -> Option<SpecAndTagInfoAndDirection> {
    let info = self.def_graph.lookup_spec_and_tag_by_alias(alias, direction.as_str());
    
    match info {
      Some(info) => {
        return Some(SpecAndTagInfoAndDirection {
          spec_name: info.spec_name,
          unique_spec_label: info.unique_spec_label,
          tag: info.tag,
          direction: direction,
        });
      }
      None => {
        return None;
      }
    }
  
  }
  
}

#[napi]
pub fn load_def_graph_from_json(json: String) -> DefGraph {
  return DefGraph {
    def_graph: load_from_json_impl(json)
  };
}
