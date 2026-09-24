//! Policy family `livestack.fleet.choose_target` v1 (openspec change
//! `scheduler-policy-routine`, design.md §2): the per-job target choice inside
//! `node-py/livestack_node/fleet_scheduler.py` `schedule()`.
//!
//! A faithful port of `_feasible_candidates`, `_score`, `_distance_n`,
//! `_utilization_n` and `_norm`, operation for operation, so a Python-vs-Rust
//! differential test can demand scores within 1e-12. State that `schedule()`
//! mutates ACROSS jobs (free capacity, pool headroom, usage/quotas) stays in
//! Python and arrives here as per-candidate booleans.
//!
//! Any change that could alter a decision for some input bumps `VERSION`.

use serde::{Deserialize, Serialize};

use jingway_policy::artifact::{ParamSpace, ParamSpec, Params, ParamsExt, Search};
use jingway_policy::family::{Candidate, Evaluation, PolicyFamily, Row};

/// `fleet_scheduler._EPS`.
const EPS: f64 = 1e-9;

pub struct ChooseTarget;

jingway_policy::registry!(LivestackFamilies, [ChooseTarget]);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Sla {
    Interactive,
    Normal,
    Batch,
}

impl Sla {
    fn name(self) -> &'static str {
        match self {
            Sla::Interactive => "interactive",
            Sla::Normal => "normal",
            Sla::Batch => "batch",
        }
    }
}

/// `fleet_scheduler.Tier`, cost-ascending (the derive order IS the order).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Tier {
    Local,
    Spot,
    Ondemand,
    LastResort,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub sla: Sla,
    pub created_at: f64,
    #[serde(default)]
    pub deadline: Option<f64>,
    pub est_duration_s: f64,
    #[serde(default)]
    pub locality_host: Option<String>,
}

/// design.md §2.1.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ChooseTargetContext {
    pub now: f64,
    pub job: Job,
}

/// design.md §2.2. Booleans that do not apply to a candidate (e.g. `fits_now`
/// for a pool) are `false` and never read.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TargetFeatures {
    pub host_id: String,
    pub tier: Tier,
    pub running: bool,
    pub elastic: bool,
    pub selector_match: bool,
    pub fits_now: bool,
    pub headroom_ok: bool,
    pub fits_instance: bool,
    pub provision_latency_s: f64,
    pub cost_per_hour: f64,
    pub cost_per_job: f64,
    #[serde(default)]
    pub distance_ms: Option<f64>,
    #[serde(default)]
    pub utilization: Option<f64>,
}

/// `fleet_scheduler._norm`.
fn norm(v: f64, xs: &[f64]) -> f64 {
    // Python min()/max(): keep the first extreme; replace only on strict < / >.
    let mut lo = xs[0];
    let mut hi = xs[0];
    for &x in &xs[1..] {
        if x < lo {
            lo = x;
        }
        if x > hi {
            hi = x;
        }
    }
    if hi - lo < EPS {
        return 0.0;
    }
    (v - lo) / (hi - lo)
}

fn py_max(xs: &[f64]) -> f64 {
    let mut hi = xs[0];
    for &x in &xs[1..] {
        if x > hi {
            hi = x;
        }
    }
    hi
}

/// `_distance_n`: unmeasured scores as the farthest measured.
fn distance_n(v: Option<f64>, all: &[Option<f64>]) -> f64 {
    let mut known: Vec<f64> = all.iter().filter_map(|x| *x).collect();
    if known.is_empty() {
        return 0.0;
    }
    let value = v.unwrap_or_else(|| py_max(&known));
    known.push(value);
    norm(value, &known)
}

/// `_utilization_n`: unreported scores as the UPPER median of the reported.
fn utilization_n(v: Option<f64>, all: &[Option<f64>]) -> f64 {
    let mut known: Vec<f64> = all.iter().filter_map(|x| *x).collect();
    if known.is_empty() {
        return 0.0;
    }
    let mut ordered = known.clone();
    ordered.sort_by(|a, b| a.partial_cmp(b).expect("finite utilization"));
    let median = ordered[ordered.len() / 2];
    let value = v.unwrap_or(median);
    known.push(value);
    norm(value, &known)
}

/// A candidate that passed the feasibility checks (Python `_Cand`).
struct Feasible {
    idx: usize,
    est_cost: f64,
    eta: f64,
    local: f64,
}

fn filtered(id: &str, code: &str) -> Row {
    Row { id: id.to_string(), eligible: false, score: None, explorable: false, reason: format!("filtered:{code}") }
}

impl PolicyFamily for ChooseTarget {
    const ID: &'static str = "livestack.fleet.choose_target";
    const VERSION: u32 = 1;
    const MAX_EPSILON: f64 = 0.10;
    type Context = ChooseTargetContext;
    type Features = TargetFeatures;

    /// design.md §2.3. Defaults (today's code) live in the artifact, not here.
    fn param_space() -> ParamSpace {
        use Search::{Fixed, Linear};
        ParamSpace {
            params: vec![
                ParamSpec::f64("w_resource", 0.0, 10.0, Linear),
                ParamSpec::f64("w_budget", 0.0, 10.0, Linear),
                ParamSpec::f64("w_speed", 0.0, 10.0, Linear),
                ParamSpec::f64("w_distance", 0.0, 10.0, Linear),
                ParamSpec::f64("w_utilization", 0.0, 10.0, Linear),
                ParamSpec::f64("distance_by_sla.interactive", 0.0, 5.0, Linear),
                ParamSpec::f64("distance_by_sla.normal", 0.0, 5.0, Linear),
                ParamSpec::f64("distance_by_sla.batch", 0.0, 5.0, Linear),
                ParamSpec::f64("local_bonus", 0.0, 5.0, Linear),
                ParamSpec::f64("locality_bonus", 0.0, 5.0, Linear),
                // Fixed: they define what an SLA MEANS; the improver must not move them.
                ParamSpec::f64("sla_slack_s.interactive", 1.0, 604800.0, Fixed),
                ParamSpec::f64("sla_slack_s.normal", 1.0, 604800.0, Fixed),
                ParamSpec::f64("sla_slack_s.batch", 1.0, 604800.0, Fixed),
            ],
        }
    }

    /// design.md §2.4, exactly.
    fn evaluate(params: &Params, ctx: &ChooseTargetContext, candidates: &[Candidate<TargetFeatures>]) -> Evaluation {
        let job = &ctx.job;
        let sla = job.sla.name();
        // effective_deadline()
        let dl = match job.deadline {
            Some(d) => d,
            None => job.created_at + params.f64(&format!("sla_slack_s.{sla}")),
        };

        let mut rows: Vec<Option<Row>> = vec![None; candidates.len()];
        let mut cands: Vec<Feasible> = Vec::new();
        for (i, c) in candidates.iter().enumerate() {
            let f = &c.features;
            if !f.selector_match {
                rows[i] = Some(filtered(&c.id, "selector"));
                continue;
            }
            let eta = if f.running { 0.0 } else { f.provision_latency_s };
            if ctx.now + eta > dl + EPS {
                rows[i] = Some(filtered(&c.id, "deadline"));
                continue;
            }
            if f.running {
                if !f.fits_now {
                    rows[i] = Some(filtered(&c.id, "no_room"));
                    continue;
                }
            } else if f.elastic {
                if !f.headroom_ok {
                    rows[i] = Some(filtered(&c.id, "pool_at_cap"));
                    continue;
                }
                if !f.fits_instance {
                    rows[i] = Some(filtered(&c.id, "instance_too_small"));
                    continue;
                }
            } else {
                rows[i] = Some(filtered(&c.id, "cold_not_elastic"));
                continue;
            }
            // CostModel.estimate: per_job + per_hour * (max(0.0, d) / 3600.0).
            // Python max(0.0, d) returns d only when d > 0.0.
            let d = if job.est_duration_s > 0.0 { job.est_duration_s } else { 0.0 };
            let est_cost = f.cost_per_job + f.cost_per_hour * (d / 3600.0);
            // _local_bonus()
            let mut local = if f.tier == Tier::Local { params.f64("local_bonus") } else { 0.0 };
            if job.locality_host.as_deref().is_some_and(|h| h == f.host_id) {
                local += params.f64("locality_bonus");
            }
            cands.push(Feasible { idx: i, est_cost, eta, local });
        }

        // RunPod last-resort guard: keep LAST_RESORT only if no cheaper tier is feasible.
        let cheaper_exists = cands.iter().any(|c| candidates[c.idx].features.tier < Tier::LastResort);
        if cheaper_exists {
            cands.retain(|c| {
                let lr = candidates[c.idx].features.tier == Tier::LastResort;
                if lr {
                    rows[c.idx] = Some(filtered(&candidates[c.idx].id, "last_resort_guard"));
                }
                !lr
            });
        }

        // _score over the post-guard set E.
        let w_resource = params.f64("w_resource");
        let w_budget = params.f64("w_budget");
        let w_speed = params.f64("w_speed");
        let w_distance = params.f64("w_distance");
        let w_utilization = params.f64("w_utilization");
        let d_scale = params.f64(&format!("distance_by_sla.{sla}"));
        let costs: Vec<f64> = cands.iter().map(|c| c.est_cost).collect();
        let etas: Vec<f64> = cands.iter().map(|c| c.eta).collect();
        let dists: Vec<Option<f64>> = cands.iter().map(|c| candidates[c.idx].features.distance_ms).collect();
        let utils: Vec<Option<f64>> = cands.iter().map(|c| candidates[c.idx].features.utilization).collect();
        for c in &cands {
            let f = &candidates[c.idx].features;
            let cost_n = norm(c.est_cost, &costs);
            let eta_n = norm(c.eta, &etas);
            let dist_n = distance_n(f.distance_ms, &dists);
            let util_n = utilization_n(f.utilization, &utils);
            // Python association, left to right; `w_distance * d_scale` first.
            let score = w_budget * cost_n + w_speed * eta_n + w_distance * d_scale * dist_n + w_utilization * util_n
                - w_resource * c.local;
            rows[c.idx] = Some(Row {
                id: candidates[c.idx].id.clone(),
                eligible: true,
                score: Some(score),
                // Exploration may move a job between RUNNING machines only: it can
                // never spend money by provisioning, and never pick RunPod.
                explorable: f.running && f.tier != Tier::LastResort,
                reason: format!(
                    "scored:{score:.6} cost_n={cost_n:.6} eta_n={eta_n:.6} dist_n={dist_n:.6} util_n={util_n:.6} local={:.6}",
                    c.local
                ),
            });
        }

        Evaluation { rows: rows.into_iter().map(|r| r.expect("every candidate gets a row")).collect(), escalate: None }
    }
}
