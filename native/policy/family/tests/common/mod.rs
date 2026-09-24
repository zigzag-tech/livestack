//! Shared test helpers: artifacts and candidates for `livestack.fleet.choose_target`.
#![allow(dead_code)]

use std::collections::BTreeMap;

use jingway_policy::artifact::{
    ExplorationSpec, FamilyRef, LoadedArtifact, PolicyArtifact, Provenance, RawParam, ARTIFACT_SCHEMA,
};
use jingway_policy::family::{load, Candidate, PolicyFamily};
use livestack_policy_family::*;

/// design.md §2.3 defaults (= today's `SchedulerPolicy()` exactly).
pub fn default_params() -> BTreeMap<String, f64> {
    [
        ("w_resource", 1.0),
        ("w_budget", 1.0),
        ("w_speed", 1.0),
        ("w_distance", 2.0),
        ("w_utilization", 1.0),
        ("distance_by_sla.interactive", 1.0),
        ("distance_by_sla.normal", 0.5),
        ("distance_by_sla.batch", 0.1),
        ("local_bonus", 1.0),
        ("locality_bonus", 0.5),
        ("sla_slack_s.interactive", 30.0),
        ("sla_slack_s.normal", 1800.0),
        ("sla_slack_s.batch", 43200.0),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v))
    .collect()
}

pub fn artifact(params: &BTreeMap<String, f64>, exploration: ExplorationSpec) -> PolicyArtifact {
    PolicyArtifact {
        schema: ARTIFACT_SCHEMA.into(),
        policy_id: "livestack.fleet.choose_target".into(),
        family: FamilyRef { id: ChooseTarget::ID.into(), version: ChooseTarget::VERSION },
        version: String::new(),
        parent_version: None,
        params: params.iter().map(|(k, v)| (k.clone(), RawParam::Number(*v))).collect(),
        exploration,
        provenance: Provenance { created_by: "human:test".into(), created_at: "2026-09-24T00:00:00Z".into(), notes: None },
    }
    .with_computed_version()
}

pub fn loaded(params: &BTreeMap<String, f64>, exploration: ExplorationSpec) -> LoadedArtifact {
    load::<ChooseTarget>(&artifact(params, exploration)).expect("valid artifact")
}

pub fn ctx(sla: Sla) -> ChooseTargetContext {
    ChooseTargetContext {
        now: 1000.0,
        job: Job { id: "j".into(), sla, created_at: 990.0, deadline: None, est_duration_s: 60.0, locality_host: None },
    }
}

/// A running target with room, selector matching, zero cost.
pub fn running(id: &str, tier: Tier) -> Candidate<TargetFeatures> {
    Candidate {
        id: id.into(),
        features: TargetFeatures {
            host_id: id.into(),
            tier,
            running: true,
            elastic: false,
            selector_match: true,
            fits_now: true,
            headroom_ok: false,
            fits_instance: false,
            provision_latency_s: 0.0,
            cost_per_hour: 0.0,
            cost_per_job: 0.0,
            distance_ms: None,
            utilization: None,
        },
    }
}

/// An elastic pool with headroom whose instance fits the job.
pub fn pool(id: &str, tier: Tier, provision_latency_s: f64) -> Candidate<TargetFeatures> {
    let mut c = running(id, tier);
    c.features.running = false;
    c.features.elastic = true;
    c.features.fits_now = false;
    c.features.headroom_ok = true;
    c.features.fits_instance = true;
    c.features.provision_latency_s = provision_latency_s;
    c
}
