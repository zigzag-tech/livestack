use pyo3::prelude::*;
// use shared::systems::system_a:: { do_stuff_from_system_a }; // or (1)
// use shared::systems::system_a::*; // or this one (1)
use livestack_shared::systems::def_graph_utils::{unique_spec_identifier as unique_spec_identifier_impl};
/// Formats the sum of two numbers as string.

// #[pyfunction]
// fn unique_spec_identifier(a: String, b: Option<String>) -> PyResult<String> {
//     Ok(unique_spec_identifier_impl(a, b))
// }

// /// A Python module implemented in Rust.
// #[pymodule]
// fn shared_py(_py: Python, m: &PyModule) -> PyResult<()> {
//     m.add_function(wrap_pyfunction!(unique_spec_identifier, m)?)?;
//     Ok(())
// }
