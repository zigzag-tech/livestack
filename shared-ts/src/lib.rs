#![deny(clippy::all)]
use napi_derive::napi;


use livestack_shared::systems::def_graph_utils::{
  unique_spec_identifier,
};


// #[macro_use]
// extern crate napi_derive;

#[napi]
pub fn gen_spec_identifier(a: String, b: Option<String>) -> String {
  return unique_spec_identifier(a, b);
}

