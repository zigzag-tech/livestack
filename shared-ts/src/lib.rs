#![deny(clippy::all)]
use napi::JsObject;
use napi_derive::napi;


use livestack_shared::systems::def_graph_utils::{
  unique_spec_identifier,
  unique_stream_identifier as unique_stream_identifier_impl, SpecTagInfo,
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
      Some(SpecTagInfo {
        spec_name: from.spec_name,
        unique_spec_label: from.unique_spec_label,
        tag: from.tag,
      })
    }
    None => None,
  };
  let to = match p.to {
    Some(to) => {
      Some(SpecTagInfo {
        spec_name: to.spec_name,
        unique_spec_label: to.unique_spec_label,
        tag: to.tag,
      })
    }
    None => None,
  };
  return unique_stream_identifier_impl(from, to);
}