use livestack_shared::models::ModelA;
use pyo3::prelude::*;
// use shared::systems::system_a:: { do_stuff_from_system_a }; // or (1)
// use shared::systems::system_a::*; // or this one (1)
use livestack_shared::systems::system_a::sum_as_string_impl;
use livestack_shared::systems::*; // or this one (2) // or (1)
/// Formats the sum of two numbers as string.

#[pyfunction]
fn sum_as_string(a: usize, b: usize) -> PyResult<String> {
    Ok(sum_as_string_impl(a, b).to_string())
}

/// A Python module implemented in Rust.
#[pymodule]
fn shared(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum_as_string, m)?)?;
    Ok(())
}
