//! Invariant fixtures (design.md §4, the Jingway ladder's cornerstones rung):
//! `tests/fixtures/livestack.fleet.choose_target/invariants/*.json`, format
//! `{"name", "statement", "context", "candidates", "expect": {"never_chosen": [ids]}}`.
//! Each must hold for ANY params within the hard bounds and any allowed
//! exploration, so this sweeps corners of the param space plus seeded random
//! points, with exploration off and at MAX_EPSILON with a margin wide enough to
//! admit every eligible candidate.
//!
//! The same fixtures also run through the CLI's `replay --expect` (Jingway task
//! 6.3) in `cli/tests/cli.rs`; this sweep covers far more of the param space.

mod common;

use std::collections::{BTreeMap, HashSet};

use common::*;
use jingway_policy::artifact::{ExplorationSpec, ParamKind};
use jingway_policy::decide::decide;
use jingway_policy::family::{Candidate, PolicyFamily};
use livestack_policy_family::*;
use serde_json::Value;

/// xorshift64*: deterministic, dependency-free.
struct Rng(u64);
impl Rng {
    fn unit(&mut self) -> f64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        ((self.0.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 11) as f64) / ((1u64 << 53) as f64)
    }
}

fn param_sets() -> Vec<BTreeMap<String, f64>> {
    let space = ChooseTarget::param_space();
    let mut sets = vec![default_params()];
    // Every param at its min, every param at its max.
    for pick in [0, 1] {
        sets.push(
            space.params.iter().map(|p| (p.name.clone(), if pick == 0 { p.hard_min } else { p.hard_max })).collect(),
        );
    }
    // One weight at max, the rest at min (each objective alone).
    for hot in &space.params {
        sets.push(
            space.params.iter().map(|p| (p.name.clone(), if p.name == hot.name { p.hard_max } else { p.hard_min })).collect(),
        );
    }
    let mut rng = Rng(20260924);
    for _ in 0..300 {
        sets.push(
            space
                .params
                .iter()
                .map(|p| {
                    assert_eq!(p.kind, ParamKind::F64);
                    (p.name.clone(), p.hard_min + rng.unit() * (p.hard_max - p.hard_min))
                })
                .collect(),
        );
    }
    sets
}

#[test]
fn invariant_fixtures_hold_for_any_params() {
    let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/livestack.fleet.choose_target/invariants");
    let mut paths: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    paths.sort();
    assert_eq!(paths.len(), 4, "design.md §4 names four invariants: {paths:?}");
    let sets = param_sets();
    let explorations = [ExplorationSpec::OFF, ExplorationSpec { enabled: true, epsilon: ChooseTarget::MAX_EPSILON, margin: 1000.0 }];
    for path in &paths {
        let fx: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        assert!(!fx["statement"].as_str().unwrap_or("").is_empty(), "{}: statement", path.display());
        let ctx: ChooseTargetContext = serde_json::from_value(fx["context"].clone()).unwrap();
        let cands: Vec<Candidate<TargetFeatures>> = serde_json::from_value(fx["candidates"].clone()).unwrap();
        let never: Vec<String> = serde_json::from_value(fx["expect"]["never_chosen"].clone()).unwrap();
        assert!(!never.is_empty());
        let mut chosen_any = HashSet::new();
        let mut explored_any = false;
        for params in &sets {
            for ex in explorations {
                let art = loaded(params, ex);
                for i in 0..40 {
                    let d = decide::<ChooseTarget>(&art, &ctx, &cands, &format!("inv-{i}")).unwrap();
                    let chosen = d.chosen.clone().expect("fixtures always have an eligible candidate");
                    assert!(!never.contains(&chosen), "{}: {chosen} chosen under {params:?} {ex:?}", path.display());
                    assert!(!d.explore_set.iter().any(|s| never.contains(s)), "{}: explore_set {:?}", path.display(), d.explore_set);
                    explored_any |= d.explored;
                    chosen_any.insert(chosen);
                }
            }
        }
        // Positive controls: the sweep moved the choice, and exploration fired.
        assert!(chosen_any.len() > 1, "{}: the param sweep never moved the choice ({chosen_any:?})", path.display());
        assert!(explored_any, "{}: exploration never fired, so the fixture tested nothing about it", path.display());
    }
}
