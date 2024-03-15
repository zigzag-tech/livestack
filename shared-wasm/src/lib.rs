mod utils;

use wasm_bindgen::prelude::{wasm_bindgen, JsValue};
use serde::{Deserialize, Serialize};
use serde_wasm_bindgen;
use livestack_shared::systems::def_graph::{
    DefGraph as DefGraphImpl,
    NodeType as NodeTypeImpl,
    // Other necessary imports from def_graph
};
use livestack_shared::systems::def_graph_utils::{
    unique_spec_identifier as unique_spec_identifier_impl,
};
use livestack_shared::systems::def_graph_utils::{
    // Necessary imports from def_graph_utils
};

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

#[wasm_bindgen]
pub fn gen_spec_identifier(spec_name: String, unique_spec_label: Option<String>) -> String {
    unique_spec_identifier_impl(spec_name, unique_spec_label)
}

// ... (Other functions go here, similar to the napi version)

// Utility functions for serialization and deserialization if needed
// fn serialize<T: Serialize>(value: &T) -> JsValue {
//     serde_wasm_bindgen::to_value(value).unwrap()
// }

// fn deserialize<'a, T: Deserialize<'a>>(value: &JsValue) -> T {
//     serde_wasm_bindgen::from_value(value.clone()).unwrap()
// }
