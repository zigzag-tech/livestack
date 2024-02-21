#![deny(clippy::all)]
use livestack_shared::systems::system_a::sum_as_string_impl; // or (1)

#[macro_use]
extern crate napi_derive;

#[napi]
pub fn sum_as_string(a: i32, b: i32) -> String {
  return sum_as_string_impl(a as usize, b as usize);
}
