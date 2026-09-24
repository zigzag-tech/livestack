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

#[path = "../../family/tests/common/mod.rs"]
mod common;

/// design.md §4's four invariant fixtures through `replay --expect` (the Jingway
/// ladder's cornerstones rung calls exactly this), under the defaults, every
/// param at its min, every param at its max, each with exploration off and at
/// MAX_EPSILON with a margin admitting every eligible candidate.
#[test]
fn cli_invariant_fixtures_hold_via_replay_expect() {
    use jingway_policy::artifact::ExplorationSpec;
    use jingway_policy::cli::EXIT_EXPECT;
    use jingway_policy::family::PolicyFamily;
    use livestack_policy_family::ChooseTarget;

    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    let fixtures = root.join("family/tests/fixtures/livestack.fleet.choose_target/invariants");
    let dir = std::env::temp_dir().join(format!("livestack-policy-expect-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let space = ChooseTarget::param_space();
    let bound = |max: bool| space.params.iter().map(|p| (p.name.clone(), if max { p.hard_max } else { p.hard_min })).collect();
    let explore = ExplorationSpec { enabled: true, epsilon: ChooseTarget::MAX_EPSILON, margin: 1000.0 };
    let mut args: Vec<String> = vec!["replay".into()];
    let mut n = 0;
    for params in [common::default_params(), bound(false), bound(true)] {
        for ex in [ExplorationSpec::OFF, explore] {
            let p = dir.join(format!("a{n}.json"));
            std::fs::write(&p, serde_json::to_vec(&common::artifact(&params, ex)).unwrap()).unwrap();
            args.extend(["--artifact".into(), p.display().to_string()]);
            n += 1;
        }
    }
    let run_expect = |pattern: String| {
        let mut a = args.clone();
        a.extend(["--expect".into(), pattern]);
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run(&LivestackFamilies, &a, &mut out, &mut err);
        (code, serde_json::from_slice::<serde_json::Value>(&out).unwrap_or_default(), String::from_utf8(err).unwrap())
    };

    let (code, v, err) = run_expect(fixtures.join("*.json").display().to_string());
    assert_eq!(code, EXIT_OK, "{v} {err}");
    assert_eq!(v["expect"]["passed"], true, "{v}");
    let results = v["expect"]["results"].as_array().unwrap();
    assert_eq!(results.len(), 4 * n, "four fixtures under {n} artifacts: {v}");
    assert!(results.iter().all(|r| r["passed"] == true));

    // Positive control: fixture 01 with every eligible candidate forbidden.
    let mut fx: serde_json::Value =
        serde_json::from_slice(&std::fs::read(fixtures.join("01_last_resort_never_while_cheaper_feasible.json")).unwrap()).unwrap();
    fx["name"] = "control_everything_forbidden".into();
    fx["expect"]["never_chosen"] = serde_json::json!(["tower0", "aliyun-spot", "runpod"]);
    let control = dir.join("control.json");
    std::fs::write(&control, serde_json::to_vec(&fx).unwrap()).unwrap();
    let (code, v, err) = run_expect(control.display().to_string());
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(code, EXIT_EXPECT, "{v} {err}");
    assert_eq!(v["expect"]["passed"], false);
}
