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
pub fn def_graph(root_spec_name: String, input_tags: Vec<String>, output_tags: Vec<String>) -> DefGraph {
  DefGraph::new(root_spec_name, input_tags, output_tags)
}

