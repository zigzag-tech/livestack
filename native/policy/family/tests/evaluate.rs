//! One test per reason code of design.md §2.4, the explorable rule, and a
//! golden score computed by `fleet_scheduler._score` itself.

mod common;

use common::*;
use jingway_policy::artifact::ExplorationSpec;
use jingway_policy::decide::decide;
use jingway_policy::family::{Candidate, PolicyFamily, Row};
use livestack_policy_family::*;

fn eval(ctx: &ChooseTargetContext, cands: &[Candidate<TargetFeatures>]) -> Vec<Row> {
    let art = loaded(&default_params(), ExplorationSpec::OFF);
    ChooseTarget::evaluate(art.params(), ctx, cands).rows
}

fn code(r: &Row) -> &str {
    r.reason.split(' ').next().unwrap()
}

/// `target` gets `reason`, is ineligible with no score, and a plain running
/// sibling stays eligible (positive control).
fn assert_filtered(ctx: &ChooseTargetContext, target: Candidate<TargetFeatures>, reason: &str) {
    let rows = eval(ctx, &[target, running("ok", Tier::Local)]);
    assert_eq!(code(&rows[0]), reason, "{:?}", rows[0]);
    assert!(!rows[0].eligible && rows[0].score.is_none() && !rows[0].explorable);
    assert!(rows[1].eligible, "control candidate: {:?}", rows[1]);
}

#[test]
fn reason_selector() {
    let mut c = running("x", Tier::Local);
    c.features.selector_match = false;
    assert_filtered(&ctx(Sla::Normal), c, "filtered:selector");
}

#[test]
fn reason_deadline_from_sla_slack_and_explicit_deadline() {
    // interactive slack 30: deadline 990 + 30 = 1020; now 1000 + eta must be <= 1020 + 1e-9.
    assert_filtered(&ctx(Sla::Interactive), pool("x", Tier::Spot, 20.5), "filtered:deadline");
    let rows = eval(&ctx(Sla::Interactive), &[pool("x", Tier::Spot, 20.0)]);
    assert!(rows[0].eligible, "exactly at the deadline is feasible: {:?}", rows[0]);
    let mut c = ctx(Sla::Batch);
    c.job.deadline = Some(1005.0); // explicit deadline wins over the 12 h batch slack
    assert_filtered(&c, pool("x", Tier::Spot, 6.0), "filtered:deadline");
}

#[test]
fn reason_no_room() {
    let mut c = running("x", Tier::Spot);
    c.features.fits_now = false;
    assert_filtered(&ctx(Sla::Normal), c, "filtered:no_room");
}

#[test]
fn reason_pool_at_cap() {
    let mut c = pool("x", Tier::Spot, 5.0);
    c.features.headroom_ok = false;
    assert_filtered(&ctx(Sla::Normal), c, "filtered:pool_at_cap");
}

#[test]
fn reason_instance_too_small() {
    let mut c = pool("x", Tier::Spot, 5.0);
    c.features.fits_instance = false;
    assert_filtered(&ctx(Sla::Normal), c, "filtered:instance_too_small");
}

#[test]
fn reason_cold_not_elastic() {
    let mut c = pool("x", Tier::Spot, 5.0);
    c.features.elastic = false;
    assert_filtered(&ctx(Sla::Normal), c, "filtered:cold_not_elastic");
}

#[test]
fn reason_last_resort_guard_only_while_a_cheaper_tier_is_feasible() {
    assert_filtered(&ctx(Sla::Normal), running("runpod", Tier::LastResort), "filtered:last_resort_guard");
    // Alone (the cheaper one is infeasible), LAST_RESORT is eligible but never explorable.
    let mut full = running("local", Tier::Local);
    full.features.fits_now = false;
    let rows = eval(&ctx(Sla::Normal), &[full, running("runpod", Tier::LastResort)]);
    assert_eq!(code(&rows[0]), "filtered:no_room");
    assert!(rows[1].eligible && !rows[1].explorable, "{:?}", rows[1]);
    assert!(code(&rows[1]).starts_with("scored:"));
}

#[test]
fn reason_scored_and_explorable_only_for_running() {
    let rows = eval(&ctx(Sla::Normal), &[running("r", Tier::Spot), pool("p", Tier::Spot, 5.0)]);
    assert!(rows.iter().all(|r| r.eligible && r.reason.starts_with("scored:")));
    assert!(rows[0].explorable && !rows[1].explorable);
    // eta_n: running 0, pool 1; all else equal -> score 0 vs 1 (w_speed = 1).
    assert_eq!(rows[0].score, Some(0.0));
    assert_eq!(rows[1].score, Some(1.0));
    assert_eq!(rows[1].reason, "scored:1.000000 cost_n=0.000000 eta_n=1.000000 dist_n=0.000000 util_n=0.000000 local=0.000000");
}

/// Expected values printed by `fleet_scheduler._score` (Python) on the same
/// inputs; bit-exact.
#[test]
fn golden_scores_match_python_bit_for_bit() {
    let mut p = default_params();
    for (k, v) in [
        ("w_resource", 1.3),
        ("w_budget", 0.7),
        ("w_speed", 2.1),
        ("w_distance", 2.9),
        ("w_utilization", 1.7),
        ("distance_by_sla.interactive", 0.77),
        ("local_bonus", 1.1),
        ("locality_bonus", 0.45),
    ] {
        p.insert(k.into(), v);
    }
    let art = loaded(&p, ExplorationSpec::OFF);
    let mut c = ctx(Sla::Interactive);
    c.job.est_duration_s = 90.0;
    c.job.locality_host = Some("b".into());
    let mut a = running("a", Tier::Local);
    a.features.distance_ms = Some(16.2);
    a.features.utilization = Some(0.4);
    let mut b = running("b", Tier::Spot);
    b.features.cost_per_hour = 0.37;
    b.features.cost_per_job = 0.001;
    b.features.utilization = Some(0.9);
    let mut cc = pool("c", Tier::Ondemand, 12.5);
    cc.features.cost_per_hour = 1.13;
    cc.features.distance_ms = Some(180.7);
    let mut d = running("d", Tier::Ondemand);
    d.features.cost_per_hour = 0.9;
    d.features.distance_ms = Some(3.3);
    d.features.utilization = Some(0.1);
    let dec = decide::<ChooseTarget>(&art, &c, &[a, b, cc, d], "golden").unwrap();
    let scores: Vec<f64> = dec.rows.iter().map(|r| r.score.unwrap()).collect();
    assert_eq!(scores, vec![-0.6301228861330328, 3.6019823008849556, 5.6705, 0.5575221238938054]);
    assert_eq!(dec.chosen.as_deref(), Some("a"), "schedule() admits on a");
}

#[test]
fn ties_go_to_the_earliest_candidate() {
    let art = loaded(&default_params(), ExplorationSpec::OFF);
    let dec = decide::<ChooseTarget>(&art, &ctx(Sla::Normal), &[running("first", Tier::Spot), running("second", Tier::Spot)], "t").unwrap();
    assert_eq!(dec.chosen.as_deref(), Some("first"));
}

#[test]
fn registry_lists_the_family() {
    use jingway_policy::family::FamilyRegistry;
    let fams = LivestackFamilies.families();
    assert_eq!((fams[0].0, fams[0].1), ("livestack.fleet.choose_target", 1));
    assert_eq!(fams[0].2.params.len(), 13);
    let mut too_eager = default_params();
    too_eager.insert("w_budget".into(), 1.0);
    let bad = artifact(&too_eager, ExplorationSpec { enabled: true, epsilon: 0.11, margin: 0.1 });
    assert!(LivestackFamilies.load(&bad).is_err(), "MAX_EPSILON = 0.10");
}
