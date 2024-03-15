#![allow(non_snake_case)] 

mod utils;

use wasm_bindgen::prelude::{wasm_bindgen, JsValue};
use serde::{Deserialize, Serialize};
use serde_wasm_bindgen;
use livestack_shared::systems::def_graph_utils::{
    unique_spec_identifier,
    unique_stream_identifier as unique_stream_identifier_impl, SpecTagInfo as SpecTagInfoImpl,
    FromSpecAndTag as FromSpecAndTagImpl,
    ToSpecAndTag as ToSpecAndTagImpl,
};
use livestack_shared::systems::def_graph_utils::{
    unique_spec_identifier as unique_spec_identifier_impl,
};
use livestack_shared::systems::def_graph_utils::{
    // Necessary imports from def_graph_utils
};
use tsify::Tsify;


// Define all the structs and enums that will be used in the wasm interface

#[wasm_bindgen]
#[derive(Serialize, Deserialize)]
pub struct DefGraph {
    // Fields corresponding to DefGraphImpl
}

// ... (Other structs and enums go here, similar to the napi version)

#[wasm_bindgen]
impl DefGraph {
    // ... (Existing methods and constructor)
}

#[wasm_bindgen(js_name = genSpecIdentifier)]
pub fn gen_spec_identifier(spec_name: String, unique_spec_label: Option<String>) -> String {
    unique_spec_identifier_impl(spec_name, unique_spec_label)
}

#[derive(Tsify, Serialize, Deserialize)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct SpecTagInfoParams {
    pub spec_name: String,
    pub unique_spec_label: Option<String>,
    pub tag: String,
}

#[derive(Tsify, Serialize, Deserialize)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct UniqueStreamIdentifierParams {
    pub from: Option<SpecTagInfoParams>,
    pub to: Option<SpecTagInfoParams>,
  }
  

#[wasm_bindgen(js_name = uniqueStreamIdentifier)]
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

// ... (Other functions go here, similar to the napi version)

// Utility functions for serialization and deserialization if needed
// fn serialize<T: Serialize>(value: &T) -> JsValue {
//     serde_wasm_bindgen::to_value(value).unwrap()
// }

// fn deserialize<'a, T: Deserialize<'a>>(value: &JsValue) -> T {
//     serde_wasm_bindgen::from_value(value.clone()).unwrap()
// }
