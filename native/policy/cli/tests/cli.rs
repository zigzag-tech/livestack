//! The CLI binary's registry, through the Jingway CLI entry point.

use jingway_policy::cli::{run, EXIT_OK};
use livestack_policy_family::LivestackFamilies;

#[test]
fn cli_families_lists_choose_target() {
    let (mut out, mut err) = (Vec::new(), Vec::new());
    assert_eq!(run(&LivestackFamilies, &["families".into()], &mut out, &mut err), EXIT_OK);
    let v: serde_json::Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(v[0]["id"], "livestack.fleet.choose_target");
    assert_eq!(v[0]["version"], 1);
    assert_eq!(v[0]["param_space"]["params"].as_array().unwrap().len(), 13);
}
