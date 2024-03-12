#![deny(clippy::all)]
use napi_derive::napi;

pub use livestack_shared::systems::def_graph_utils::*;
use livestack_shared::systems::system_a::sum_as_string_impl;
pub use livestack_shared::systems::def_graph::*;
pub use livestack_shared::systems::def_graph_utils::{
  unique_spec_identifier as unique_spec_identifier_impl,
  unique_stream_identifier as unique_stream_identifier_impl,
};
pub use livestack_shared::systems::instantiated_graph::*;


// #[macro_use]
// extern crate napi_derive;

#[napi]
pub fn sum_as_string(a: i32, b: i32) -> String {
  return sum_as_string_impl(a as usize, b as usize);
}



// #[napi]
// pub fn def_graph(root_spec_name: String, input_tags: Vec<String>, output_tags: Vec<String>) -> DefGraph {
//   return DefGraph::new(root_spec_name, input_tags, output_tags)
// }

