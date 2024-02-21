#![deny(clippy::all)]
use livestack_shared::systems::system_a::sum_as_string_impl;
use livestack_shared::systems::def_graph::DefGraph;
use livestack_shared::systems::def_graph::SpecBase;

#[macro_use]
extern crate napi_derive;

#[napi]
pub fn sum_as_string(a: i32, b: i32) -> String {
  return sum_as_string_impl(a as usize, b as usize);
}

#[napi]
pub fn def_graph() -> u32  {
  let root_spec = SpecBase {
    name: "RootSpec".to_string(),
    input_tags: vec![],
    output_tags: vec![],
};
   let g = DefGraph::new(root_spec);
   return g.graph.node_count().try_into().unwrap();

}